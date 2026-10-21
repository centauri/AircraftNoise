#!/usr/bin/env python3
"""
Database module for Aircraft Noise application
Handles SQLite database operations for planes, noise data, and admin settings
"""

import sqlite3
import json
import logging
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import os

RECORDED_TABLES = (
    'aircraft_events',
    'visitor_sessions',
    'page_views',
    'visitor_interactions',
    'daily_analytics',
    'api_usage_daily',
)

DEFAULT_OPERATIONAL_SETTINGS = {
    'visitor_radius_km': '8',
    'visitor_altitude_max_m': '2000',
    'visitor_update_interval_seconds': '10',
    'detection_radius_km': '8.0',
    'max_altitude_m': '2000',
    'noise_threshold_db': '50.0',
    'update_interval_seconds': '10',
    'max_events_display': '1000',
    'inner_zone_type': 'circle',
    'detection_zone_points': '[]',
    'polygon_area_km2': '0',
}

class PlaneNoiseDB:
    def __init__(self, db_path: str = "plane_noise.db"):
        self.db_path = db_path
        self.init_database()
    
    def _deduplicate_aircraft_events(self, conn: sqlite3.Connection):
        """Remove exact duplicates within the same (icao24,type,bucket_5m) keeping best metrics.
        Best metrics = min(distance), max(spl_db), latest timestamp; prefer non-null metadata.
        """
        try:
            c = conn.cursor()
            # Compute winners per key
            c.execute('''
                WITH ranked AS (
                  SELECT 
                    id, icao24, type, bucket_5m,
                    distance_km,
                    spl_db,
                    timestamp,
                    ROW_NUMBER() OVER (
                      PARTITION BY icao24, type, bucket_5m
                      ORDER BY 
                        (CASE WHEN distance_km IS NULL THEN 1 ELSE 0 END), distance_km ASC,
                        (CASE WHEN spl_db IS NULL THEN 1 ELSE 0 END), spl_db DESC,
                        timestamp DESC
                    ) AS rn
                  FROM aircraft_events
                )
                SELECT id FROM ranked WHERE rn > 1
            ''')
            ids_to_delete = [row[0] for row in c.fetchall()]
            if ids_to_delete:
                # Delete duplicates
                c.execute(
                    f"DELETE FROM aircraft_events WHERE id IN ({','.join(['?']*len(ids_to_delete))})",
                    ids_to_delete
                )
                conn.commit()
                logging.info(f"Deduplicated {len(ids_to_delete)} aircraft_event rows")
        except Exception as e:
            logging.warning(f"Deduplication failed: {e}")

    def init_database(self):
        """Initialize the database with required tables"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Admin settings table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS admin_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Visitor statistics: daily totals, hours and per-kind counts. No
            # addresses, no identifiers; bounded by analytics.RETAIN_DAYS.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stats_days (
                    day TEXT PRIMARY KEY,
                    views INTEGER NOT NULL DEFAULT 0,
                    api_views INTEGER NOT NULL DEFAULT 0,
                    visitors INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stats_hours (
                    hour_ts INTEGER PRIMARY KEY,
                    day TEXT NOT NULL,
                    views INTEGER NOT NULL DEFAULT 0,
                    visitors INTEGER NOT NULL DEFAULT 0
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stats_dims (
                    day TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    key TEXT NOT NULL,
                    n INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, kind, key)
                )
            ''')

            # Addresses the scraper guard blocked, kept across restarts
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS guard_bans (
                    ip TEXT PRIMARY KEY,
                    until_ts REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    bans INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Insert default settings if they don't exist
            default_settings = [
                ('visitor_radius_km', '8'),
                ('visitor_altitude_max_m', '2000'),
                ('visitor_update_interval_seconds', '10'),
                ('sensor_id', '69616'),
                ('sensor_lat', ''),
                ('sensor_lon', ''),
                ('external_access', 'false'),
                ('server_port', '8787')
            ]
            
            for key, value in default_settings:
                cursor.execute('''
                    INSERT OR IGNORE INTO admin_settings (key, value) 
                    VALUES (?, ?)
                ''', (key, value))
            
            # Plane detection settings
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS plane_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    radius_km REAL NOT NULL DEFAULT 8.0,
                    alt_max_m INTEGER NOT NULL DEFAULT 2000,
                    noise_threshold_db REAL NOT NULL DEFAULT 50.0,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Aircraft events table (replaces JSONL)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS aircraft_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    type TEXT NOT NULL,
                    icao24 TEXT NOT NULL,
                    callsign TEXT,
                    distance_km REAL,
                    altitude_m INTEGER,
                    spl_db REAL,
                    speed_kmh INTEGER,
                    heading_deg INTEGER,
                    destination TEXT,
                    manufacturer TEXT,
                    model TEXT,
                    registration TEXT,
                    operator TEXT,
                    image_url TEXT,
                    bucket_5m INTEGER,
                    radius_km REAL,
                    alt_max_m INTEGER,
                    latitude REAL,
                    longitude REAL,
                    wx_wind_speed REAL,
                    wx_wind_from_deg REAL,
                    wx_rain INTEGER,
                    wx_temp_c REAL,
                    wx_temp_2m_c REAL,
                    wx_temp_80m_c REAL,
                    wx_lat REAL,
                    wx_lon REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create indexes for better performance
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_aircraft_events_timestamp ON aircraft_events(timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_aircraft_events_icao24 ON aircraft_events(icao24)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_aircraft_events_type ON aircraft_events(type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_aircraft_events_pass ON aircraft_events(icao24, type, timestamp)')

            # One row per position sample inside the detection circle: the flight path.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS aircraft_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    icao24 TEXT NOT NULL,
                    callsign TEXT,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    altitude_m INTEGER,
                    distance_km REAL,
                    slant_km REAL,
                    heading_deg INTEGER
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_aircraft_samples_icao_ts ON aircraft_samples(icao24, ts)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_aircraft_samples_ts ON aircraft_samples(ts)')

            # Airports around the sensor and their runway ends, from OurAirports.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS airports (
                    icao TEXT PRIMARY KEY,
                    name TEXT,
                    type TEXT,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    distance_km REAL,
                    municipality TEXT,
                    country TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS runway_ends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    airport TEXT NOT NULL,
                    ident TEXT NOT NULL,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    heading REAL NOT NULL,
                    length_m REAL,
                    opposite TEXT
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_runway_ends_airport ON runway_ends(airport)')

            # Local aircraft registry: hex code to registration, type and operator.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS aircraft_registry (
                    icao24 TEXT PRIMARY KEY,
                    registration TEXT,
                    typecode TEXT,
                    type_name TEXT,
                    manufacturer TEXT,
                    model TEXT,
                    operator TEXT,
                    built TEXT
                )
            ''')

            # --- Migration: add bucket_5m column if missing and unique index to prevent duplicates ---
            try:
                cursor.execute("PRAGMA table_info(aircraft_events)")
                cols = [row[1] for row in cursor.fetchall()]
                if 'bucket_5m' not in cols:
                    cursor.execute('ALTER TABLE aircraft_events ADD COLUMN bucket_5m INTEGER')
                    cursor.execute('UPDATE aircraft_events SET bucket_5m = CAST(timestamp/300 AS INTEGER) WHERE bucket_5m IS NULL')
            except Exception as e:
                logging.warning(f"bucket_5m migration check failed: {e}")

            # Existing databases already have the table, so CREATE TABLE does not add these.
            try:
                cursor.execute("PRAGMA table_info(aircraft_events)")
                cols = [row[1] for row in cursor.fetchall()]
                weather_columns = (
                    ('wx_wind_speed', 'REAL'),
                    ('wx_wind_from_deg', 'REAL'),
                    ('wx_rain', 'INTEGER'),
                    ('wx_temp_c', 'REAL'),
                    ('wx_temp_2m_c', 'REAL'),
                    ('wx_temp_80m_c', 'REAL'),
                    ('wx_lat', 'REAL'),
                    ('wx_lon', 'REAL'),
                    ('spl_peak_ts', 'REAL'),
                    ('spl_max_db', 'REAL'),
                    ('route_kind', 'TEXT'),
                    ('route_airport', 'TEXT'),
                    ('route_runway', 'TEXT'),
                    ('route_label', 'TEXT'),
                    ('route_confidence', 'REAL'),
                    ('in_zone', 'INTEGER'),
                )
                for name, col_type in weather_columns:
                    if name not in cols:
                        cursor.execute(f'ALTER TABLE aircraft_events ADD COLUMN {name} {col_type}')
            except Exception as e:
                logging.warning(f"weather column migration check failed: {e}")

            # Unique constraint to avoid duplicate rows per aircraft/type within 5-minute window
            try:
                cursor.execute('''
                    CREATE UNIQUE INDEX IF NOT EXISTS uniq_aircraft_bucket
                    ON aircraft_events(icao24, type, bucket_5m)
                ''')
            except Exception as e:
                logging.warning(f"Unique index creation failed (likely duplicates exist): {e}")
                try:
                    self._deduplicate_aircraft_events(conn)
                    cursor.execute('''
                        CREATE UNIQUE INDEX IF NOT EXISTS uniq_aircraft_bucket
                        ON aircraft_events(icao24, type, bucket_5m)
                    ''')
                except Exception as e2:
                    logging.error(f"Failed to enforce uniqueness after dedup: {e2}")

            # --- Users table for admin/user management ---
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    first_name TEXT,
                    last_name TEXT,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP
                )
            ''')
            
            # Insert default admin settings
            default_settings = [
                ('detection_radius_km', '8.0'),
                ('max_altitude_m', '2000'),
                ('noise_threshold_db', '50.0'),
                ('update_interval_seconds', '10'),
                ('max_events_display', '1000')
            ]
            
            for key, value in default_settings:
                cursor.execute('''
                    INSERT OR IGNORE INTO admin_settings (key, value) 
                    VALUES (?, ?)
                ''', (key, value))
            
            # Visitor tracking tables
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS visitor_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL UNIQUE,
                    ip_hash TEXT NOT NULL,
                    user_agent_hash TEXT,
                    first_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    page_views INTEGER DEFAULT 1,
                    session_duration INTEGER DEFAULT 0,
                    device_type TEXT,
                    browser TEXT,
                    operating_system TEXT,
                    referrer TEXT,
                    referrer_type TEXT,
                    search_engine TEXT,
                    search_query TEXT,
                    country TEXT,
                    is_bounce BOOLEAN DEFAULT TRUE,
                    is_bot BOOLEAN DEFAULT FALSE,
                    bot_type TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS page_views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES visitor_sessions(session_id),
                    page_path TEXT NOT NULL,
                    view_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    load_time INTEGER,
                    referrer TEXT,
                    utm_source TEXT,
                    utm_medium TEXT,
                    utm_campaign TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS visitor_interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES visitor_sessions(session_id),
                    interaction_type TEXT NOT NULL,
                    interaction_data JSON,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL UNIQUE,
                    page_views INTEGER DEFAULT 0,
                    unique_visitors INTEGER DEFAULT 0,
                    sessions INTEGER DEFAULT 0,
                    avg_session_duration REAL DEFAULT 0,
                    bounce_rate REAL DEFAULT 0,
                    heatmap_clicks INTEGER DEFAULT 0,
                    modal_opens INTEGER DEFAULT 0,
                    chart_interactions INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # API usage tracking table for persistent 24-hour counting
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS api_usage_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_name TEXT NOT NULL,
                    date DATE NOT NULL,
                    daily_requests INTEGER DEFAULT 0,
                    daily_limit INTEGER,
                    remaining_requests INTEGER,
                    last_reset_timestamp INTEGER,
                    rate_limit_per_minute INTEGER,
                    pricing TEXT,
                    reset_type TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(api_name, date)
                )
            ''')
            
            # Create indexes for visitor tracking
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_visitor_sessions_session_id ON visitor_sessions(session_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_visitor_sessions_first_visit ON visitor_sessions(first_visit)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_visitor_sessions_is_bot ON visitor_sessions(is_bot)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_page_views_session_id ON page_views(session_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_page_views_timestamp ON page_views(view_timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_visitor_interactions_session_id ON visitor_interactions(session_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_visitor_interactions_timestamp ON visitor_interactions(timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_daily_analytics_date ON daily_analytics(date)')
            
            conn.commit()
            logging.info(f"Database initialized: {self.db_path}")
    
    def get_admin_setting(self, key: str, default_value: str = None) -> str:
        """Get an admin setting value"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM admin_settings WHERE key = ?', (key,))
            result = cursor.fetchone()
            return result[0] if result else default_value
    
    def set_admin_setting(self, key: str, value: str):
        """Set an admin setting value"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO admin_settings (key, value, updated_at) 
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (key, value))
            conn.commit()
    
    def get_plane_settings(self) -> List[Dict]:
        """Get all plane detection settings"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM plane_settings ORDER BY name')
            return [dict(row) for row in cursor.fetchall()]
    
    def update_plane_settings(self, settings: Dict):
        """Update plane detection settings"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE plane_settings 
                SET radius_km = ?, alt_max_m = ?, noise_threshold_db = ?, 
                    enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (
                settings['radius_km'],
                settings['alt_max_m'], 
                settings['noise_threshold_db'],
                settings['enabled'],
                settings['id']
            ))
            conn.commit()
    
    def log_aircraft_event(self, event_data: Dict) -> int:
        """Log an aircraft event to the database"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            ts = float(event_data.get('ts') or 0)
            bucket_5m = int(ts // 300) if ts else None
            cursor.execute('''
                INSERT INTO aircraft_events (
                    timestamp, type, icao24, callsign, distance_km, altitude_m,
                    spl_db, speed_kmh, heading_deg, destination, manufacturer,
                    model, registration, operator, image_url, bucket_5m
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(icao24, type, bucket_5m) DO UPDATE SET
                    distance_km = CASE
                        WHEN excluded.distance_km IS NOT NULL AND (aircraft_events.distance_km IS NULL
                          OR excluded.distance_km * excluded.distance_km
                             + (COALESCE(excluded.altitude_m, 0) / 1000.0) * (COALESCE(excluded.altitude_m, 0) / 1000.0)
                           < aircraft_events.distance_km * aircraft_events.distance_km
                             + (COALESCE(aircraft_events.altitude_m, 0) / 1000.0) * (COALESCE(aircraft_events.altitude_m, 0) / 1000.0))
                        THEN excluded.distance_km ELSE aircraft_events.distance_km END,
                    spl_db = CASE
                        WHEN excluded.spl_db IS NOT NULL AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                        THEN excluded.spl_db ELSE aircraft_events.spl_db END,
                    altitude_m = CASE
                        WHEN excluded.distance_km IS NOT NULL AND (aircraft_events.distance_km IS NULL
                          OR excluded.distance_km * excluded.distance_km
                             + (COALESCE(excluded.altitude_m, 0) / 1000.0) * (COALESCE(excluded.altitude_m, 0) / 1000.0)
                           < aircraft_events.distance_km * aircraft_events.distance_km
                             + (COALESCE(aircraft_events.altitude_m, 0) / 1000.0) * (COALESCE(aircraft_events.altitude_m, 0) / 1000.0))
                        THEN excluded.altitude_m ELSE aircraft_events.altitude_m END,
                    speed_kmh = COALESCE(excluded.speed_kmh, aircraft_events.speed_kmh),
                    heading_deg = COALESCE(excluded.heading_deg, aircraft_events.heading_deg),
                    callsign = CASE WHEN TRIM(COALESCE(excluded.callsign, '')) <> '' THEN excluded.callsign ELSE aircraft_events.callsign END,
                    destination = COALESCE(excluded.destination, aircraft_events.destination),
                    manufacturer = COALESCE(excluded.manufacturer, aircraft_events.manufacturer),
                    model = COALESCE(excluded.model, aircraft_events.model),
                    registration = COALESCE(excluded.registration, aircraft_events.registration),
                    operator = COALESCE(excluded.operator, aircraft_events.operator),
                    image_url = COALESCE(excluded.image_url, aircraft_events.image_url),
                    timestamp = MAX(aircraft_events.timestamp, excluded.timestamp)
            ''', (
                ts,
                event_data.get('type'),
                event_data.get('icao24'),
                event_data.get('callsign'),
                event_data.get('distance_km'),
                event_data.get('altitude_m'),
                event_data.get('spl_db'),
                event_data.get('speed_kmh'),
                event_data.get('heading_deg'),
                event_data.get('destination'),
                event_data.get('manufacturer'),
                event_data.get('model'),
                event_data.get('registration'),
                event_data.get('operator'),
                event_data.get('image_url'),
                bucket_5m
            ))
            conn.commit()
            # Return the id of the row for this key
            cursor.execute('''
                SELECT id FROM aircraft_events WHERE icao24 = ? AND type = ? AND bucket_5m = ?
                ORDER BY timestamp DESC LIMIT 1
            ''', (event_data.get('icao24'), event_data.get('type'), bucket_5m))
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    
    def get_recent_events(self, limit: int = 1000, event_type: str = None) -> List[Dict]:
        """Get recent aircraft events"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if event_type:
                cursor.execute('''
                    SELECT * FROM aircraft_events 
                    WHERE type = ? 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                ''', (event_type, limit))
            else:
                cursor.execute('''
                    SELECT * FROM aircraft_events 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                ''', (limit,))
            
            return [dict(row) for row in cursor.fetchall()]

    def get_event_by_id(self, event_id: int) -> Optional[Dict]:
        """Get a single aircraft event by ID"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM aircraft_events 
                WHERE id = ?
            ''', (event_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_recent_pass_for_aircraft(self, icao24: str, within_seconds: int = 900) -> Optional[Dict]:
        """Get most recent 'pass' event for an aircraft within a time window"""
        cutoff = datetime.now().timestamp() - within_seconds
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM aircraft_events
                WHERE icao24 = ? AND type = 'pass' AND timestamp >= ?
                ORDER BY timestamp DESC LIMIT 1
            ''', (icao24, cutoff))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_pass_metrics(self, event_id: int, *,
                            distance_km: Optional[float] = None,
                            spl_db: Optional[float] = None,
                            spl_peak_ts: Optional[float] = None,
                            spl_max_db: Optional[float] = None,
                            in_zone: Optional[bool] = None,
                            altitude_m: Optional[int] = None,
                            speed_kmh: Optional[int] = None,
                            heading_deg: Optional[int] = None,
                            callsign: Optional[str] = None,
                            wx_wind_speed: Optional[float] = None,
                            wx_wind_from_deg: Optional[float] = None,
                            wx_rain: Optional[int] = None,
                            wx_temp_c: Optional[float] = None,
                            wx_temp_2m_c: Optional[float] = None,
                            wx_temp_80m_c: Optional[float] = None,
                            wx_lat: Optional[float] = None,
                            wx_lon: Optional[float] = None) -> bool:
        """Update an existing pass event.

        Distance and altitude follow the sample with the smallest 3D slant range,
        so the stored pair describes one real position. Noise keeps its maximum.
        Weather and the wind-sentence position are replaced only when this call
        supplies both wx_lat and wx_lon and the new spl_db is higher than the
        stored value (or the stored value is null).
        """
        closer = """(:distance_km IS NOT NULL AND (distance_km IS NULL
                        OR :distance_km * :distance_km + (COALESCE(:altitude_m, 0) / 1000.0) * (COALESCE(:altitude_m, 0) / 1000.0)
                         < distance_km * distance_km + (COALESCE(altitude_m, 0) / 1000.0) * (COALESCE(altitude_m, 0) / 1000.0)))"""
        louder = "(:spl_db IS NOT NULL AND (spl_db IS NULL OR :spl_db > spl_db))"
        weather = f"(:wx_lat IS NOT NULL AND :wx_lon IS NOT NULL AND {louder})"
        wx_columns = ("wx_wind_speed", "wx_wind_from_deg", "wx_rain", "wx_temp_c",
                      "wx_temp_2m_c", "wx_temp_80m_c", "wx_lat", "wx_lon")
        wx_sets = ",\n".join(
            f"{col} = CASE WHEN {weather} THEN :{col} ELSE {col} END" for col in wx_columns
        )
        sql = f"""
            UPDATE aircraft_events
            SET
                distance_km = CASE WHEN {closer} THEN :distance_km ELSE distance_km END,
                altitude_m = CASE WHEN :altitude_m IS NOT NULL AND {closer} THEN :altitude_m ELSE altitude_m END,
                spl_db = CASE WHEN {louder} THEN :spl_db ELSE spl_db END,
                spl_peak_ts = CASE WHEN {louder} THEN :spl_peak_ts ELSE spl_peak_ts END,
                spl_max_db = CASE WHEN {louder} THEN :spl_max_db ELSE spl_max_db END,
                in_zone = CASE WHEN :in_zone = 1 THEN 1 ELSE COALESCE(in_zone, :in_zone) END,
                speed_kmh = COALESCE(:speed_kmh, speed_kmh),
                heading_deg = COALESCE(:heading_deg, heading_deg),
                callsign = CASE
                    WHEN :callsign IS NOT NULL AND TRIM(:callsign) <> '' THEN TRIM(UPPER(:callsign))
                    ELSE callsign END,
                {wx_sets}
            WHERE id = :id
        """
        params = {
            "id": event_id,
            "distance_km": distance_km,
            "spl_db": spl_db,
            "spl_peak_ts": spl_peak_ts,
            "spl_max_db": spl_max_db,
            "in_zone": None if in_zone is None else (1 if in_zone else 0),
            "altitude_m": altitude_m,
            "speed_kmh": speed_kmh,
            "heading_deg": heading_deg,
            "callsign": callsign,
            "wx_wind_speed": wx_wind_speed,
            "wx_wind_from_deg": wx_wind_from_deg,
            "wx_rain": wx_rain,
            "wx_temp_c": wx_temp_c,
            "wx_temp_2m_c": wx_temp_2m_c,
            "wx_temp_80m_c": wx_temp_80m_c,
            "wx_lat": wx_lat,
            "wx_lon": wx_lon,
        }
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            conn.commit()
            return cursor.rowcount > 0

    def fill_pass_identity(self, event_id: int, *,
                           manufacturer: Optional[str] = None,
                           model: Optional[str] = None,
                           registration: Optional[str] = None,
                           operator: Optional[str] = None,
                           destination: Optional[str] = None) -> bool:
        """Fill identity columns that the pass row does not have yet. Stored values stay."""
        params = {
            "id": event_id,
            "manufacturer": manufacturer,
            "model": model,
            "registration": registration,
            "operator": operator,
            "destination": destination,
        }
        if all(value is None for key, value in params.items() if key != "id"):
            return False
        sets = ",\n".join(
            f"{col} = CASE WHEN TRIM(COALESCE({col}, '')) = '' THEN :{col} ELSE {col} END"
            for col in ("manufacturer", "model", "registration", "operator", "destination")
        )
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE aircraft_events SET {sets} WHERE id = :id", params)
            conn.commit()
            return cursor.rowcount > 0

    def rows_holding_peak(self, peak_ts: float, tolerance_s: float,
                          exclude_icao24: Optional[str] = None) -> List[Dict]:
        """Rows of other aircraft whose decibel came from a peak at about this time."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, type, icao24, callsign, distance_km, altitude_m, spl_db, spl_peak_ts, timestamp
                FROM aircraft_events
                WHERE spl_peak_ts BETWEEN ? AND ?
                  AND spl_db IS NOT NULL
                  AND (? IS NULL OR icao24 <> ?)
            ''', (peak_ts - tolerance_s, peak_ts + tolerance_s, exclude_icao24, exclude_icao24))
            return [dict(row) for row in cursor.fetchall()]

    def release_noise_peak(self, icao24: str, peak_ts: float, tolerance_s: float) -> int:
        """Take a peak away from an aircraft: every row of it holding that peak loses its decibel."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE aircraft_events
                SET spl_db = NULL, spl_peak_ts = NULL
                WHERE icao24 = ? AND spl_peak_ts BETWEEN ? AND ?
            ''', (icao24, peak_ts - tolerance_s, peak_ts + tolerance_s))
            conn.commit()
            return cursor.rowcount

    def add_track_sample(self, *, ts: float, icao24: str, callsign: Optional[str],
                         latitude: float, longitude: float, altitude_m: Optional[int],
                         distance_km: Optional[float], slant_km: Optional[float],
                         heading_deg: Optional[int]) -> None:
        """Store one position sample of a flight inside the detection circle."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO aircraft_samples
                    (ts, icao24, callsign, latitude, longitude, altitude_m, distance_km, slant_km, heading_deg)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (float(ts), icao24, callsign, float(latitude), float(longitude),
                  altitude_m, distance_km, slant_km, heading_deg))
            conn.commit()

    def get_track_samples(self, icao24s: List[str], start_ts: float, end_ts: float) -> Dict[str, List[Dict]]:
        """Samples of these aircraft in the window, grouped by icao24 and ordered by time."""
        wanted = [str(code).lower() for code in icao24s if code][:100]
        if not wanted:
            return {}
        marks = ','.join('?' * len(wanted))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f'''
                SELECT ts, icao24, callsign, latitude, longitude, altitude_m, distance_km, slant_km, heading_deg
                FROM aircraft_samples
                WHERE icao24 IN ({marks}) AND ts BETWEEN ? AND ?
                ORDER BY icao24, ts
            ''', (*wanted, float(start_ts), float(end_ts))).fetchall()
        tracks: Dict[str, List[Dict]] = {}
        for row in rows:
            tracks.setdefault(row['icao24'], []).append(dict(row))
        return tracks

    def prune_track_samples(self, older_than_ts: float) -> int:
        """Drop samples older than the given time."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('DELETE FROM aircraft_samples WHERE ts < ?', (float(older_than_ts),))
            conn.commit()
            return cursor.rowcount

    def get_passes_between(self, start_ts: float, end_ts: float) -> List[Dict]:
        """Pass rows with timestamp in [start, end), the fields the daily summary needs."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT id, timestamp, icao24, callsign, spl_db, spl_max_db, model, registration, in_zone
                FROM aircraft_events
                WHERE type = 'pass' AND timestamp >= ? AND timestamp < ?
                  AND (spl_db IS NOT NULL OR in_zone = 1)
                ORDER BY timestamp
            ''', (float(start_ts), float(end_ts))).fetchall()
            return [dict(row) for row in rows]

    def get_passes_for_callsign(self, callsign: str, limit: int = 50, before_ts: Optional[float] = None) -> List[Dict]:
        """Recent passes flown under this callsign, newest first. before_ts pages back:
        only passes older than that time, so a page can ask for the next one."""
        cs = (callsign or '').strip().upper()
        if not cs:
            return []
        before = float(before_ts) if before_ts is not None else 1e12
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT id, timestamp, icao24, registration, model, operator, distance_km, altitude_m,
                       speed_kmh, heading_deg, spl_db, spl_max_db, spl_peak_ts, latitude, longitude
                FROM aircraft_events
                WHERE type = 'pass' AND UPPER(COALESCE(callsign, '')) = ? AND timestamp < ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (cs, before, max(1, min(int(limit), 200)))).fetchall()
            return [dict(row) for row in rows]

    def prune_events(self, older_than_ts: float) -> int:
        """Drop pass and closest-approach rows older than the given time."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('DELETE FROM aircraft_events WHERE timestamp < ?', (float(older_than_ts),))
            conn.commit()
            return cursor.rowcount

    # ---- airports and routes -------------------------------------------------
    def replace_airports(self, airports: List[Dict]) -> int:
        """Store the airports found around the sensor. Enabled flags of known airports survive."""
        with sqlite3.connect(self.db_path) as conn:
            kept = {row[0]: row[1] for row in conn.execute('SELECT icao, enabled FROM airports')}
            conn.execute('DELETE FROM airports')
            conn.execute('DELETE FROM runway_ends')
            for a in airports:
                default_on = 1 if a.get('type') in ('large_airport', 'medium_airport') or a.get('scheduled') else 0
                conn.execute('''INSERT INTO airports (icao, name, type, lat, lon, distance_km, municipality, country, enabled)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                             (a['icao'], a.get('name'), a.get('type'), a['lat'], a['lon'], a.get('distance_km'),
                              a.get('municipality'), a.get('country'), kept.get(a['icao'], default_on)))
                for r in a.get('runways', []):
                    conn.execute('''INSERT INTO runway_ends (airport, ident, lat, lon, heading, length_m, opposite)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                 (a['icao'], r['ident'], r['lat'], r['lon'], r['heading'], r.get('length_m'), r.get('opposite')))
            conn.commit()
            return len(airports)

    def get_airports(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM airports ORDER BY distance_km').fetchall()
            out = []
            for row in rows:
                a = dict(row)
                a['enabled'] = bool(a['enabled'])
                a['runways'] = [r[0] for r in conn.execute(
                    'SELECT ident FROM runway_ends WHERE airport = ? ORDER BY ident', (a['icao'],))]
                out.append(a)
            return out

    def set_airport_enabled(self, icao: str, enabled: bool) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('UPDATE airports SET enabled = ? WHERE icao = ?', (1 if enabled else 0, icao.upper()))
            conn.commit()
            return cursor.rowcount > 0

    def get_runway_ends(self, enabled_only: bool = True) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            sql = 'SELECT r.* FROM runway_ends r JOIN airports a ON a.icao = r.airport'
            if enabled_only:
                sql += ' WHERE a.enabled = 1'
            return [dict(row) for row in conn.execute(sql).fetchall()]

    def get_enabled_airports(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute('SELECT icao, name, lat, lon FROM airports WHERE enabled = 1 ORDER BY distance_km')]

    def set_pass_route(self, event_id: int, route: Dict) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('''
                UPDATE aircraft_events
                SET route_kind = ?, route_airport = ?, route_runway = ?, route_label = ?, route_confidence = ?
                WHERE id = ?
            ''', (route.get('kind'), route.get('airport'), route.get('runway'), route.get('label'),
                  route.get('confidence'), event_id))
            conn.commit()
            return cursor.rowcount > 0

    def get_passes_needing_route(self, since_ts: float, until_ts: float, max_confidence: float = 0.6,
                                 limit: int = 200) -> List[Dict]:
        """Passes in the window without a route yet, or with a weak one."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT id, timestamp, icao24, latitude, longitude, altitude_m, heading_deg, route_confidence
                FROM aircraft_events
                WHERE type = 'pass' AND timestamp >= ? AND timestamp < ?
                  AND (route_confidence IS NULL OR route_confidence < ?)
                ORDER BY timestamp DESC LIMIT ?
            ''', (float(since_ts), float(until_ts), float(max_confidence), int(limit))).fetchall()
            return [dict(row) for row in rows]

    def get_route_counts(self, start_ts: float, end_ts: float) -> List[Dict]:
        """Per route label: passes, measured, loud bands and night passes, in the window."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT COALESCE(route_label, 'unlabelled') AS label, route_kind, route_airport, route_runway,
                       COUNT(*) AS passes,
                       SUM(CASE WHEN spl_db IS NOT NULL THEN 1 ELSE 0 END) AS with_db,
                       SUM(CASE WHEN spl_db >= 50 THEN 1 ELSE 0 END) AS ge50,
                       SUM(CASE WHEN spl_db >= 55 THEN 1 ELSE 0 END) AS ge55,
                       SUM(CASE WHEN spl_db >= 60 THEN 1 ELSE 0 END) AS ge60,
                       MAX(spl_db) AS max_db,
                       AVG(spl_db) AS avg_db
                FROM aircraft_events
                WHERE type = 'pass' AND timestamp >= ? AND timestamp < ?
                  AND (spl_db IS NOT NULL OR in_zone = 1)
                GROUP BY label
                ORDER BY passes DESC
            ''', (float(start_ts), float(end_ts))).fetchall()
            return [dict(row) for row in rows]

    def get_measured_passes(self, since_ts: float) -> List[Dict]:
        """Passes with a peak and a type, with the slant range at the peak where a sample exists.

        A plain LEFT JOIN rather than a correlated subquery with ORDER BY: older
        SQLite builds, such as the one on Debian 12, do not resolve the outer
        column inside that ORDER BY and fail with "no such column".
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT e.id, e.timestamp, e.icao24, e.manufacturer, e.model, e.operator, e.route_kind,
                       e.spl_db, e.spl_max_db, e.distance_km, e.altitude_m, e.spl_peak_ts,
                       s.ts AS sample_ts, s.slant_km AS sample_slant_km,
                       (SELECT typecode FROM aircraft_registry r WHERE r.icao24 = e.icao24) AS typecode
                FROM aircraft_events e
                LEFT JOIN aircraft_samples s
                  ON s.icao24 = e.icao24 AND e.spl_peak_ts IS NOT NULL
                 AND s.ts BETWEEN e.spl_peak_ts - 20 AND e.spl_peak_ts + 20
                WHERE e.type = 'pass' AND e.spl_db IS NOT NULL AND e.model IS NOT NULL AND e.timestamp >= ?
            ''', (float(since_ts),)).fetchall()
        best: Dict[int, Dict] = {}
        for row in rows:
            item = dict(row)
            sample_ts, sample_slant = item.pop('sample_ts'), item.pop('sample_slant_km')
            kept = best.get(item['id'])
            if kept is None:
                item['peak_slant_km'] = sample_slant
                item['_gap'] = abs(sample_ts - item['spl_peak_ts']) if sample_ts is not None else None
                best[item['id']] = item
                continue
            if sample_ts is not None:
                gap = abs(sample_ts - item['spl_peak_ts'])
                if kept['_gap'] is None or gap < kept['_gap']:
                    kept['peak_slant_km'], kept['_gap'] = sample_slant, gap
        out = list(best.values())
        for item in out:
            item.pop('_gap', None)
        return out

    def first_pass_ts(self) -> Optional[float]:
        """When the oldest in-zone pass happened, or None."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT MIN(timestamp) FROM aircraft_events WHERE type = 'pass' AND in_zone = 1").fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def count_unflagged_passes(self) -> int:
        """Passes with a position that were never judged against the zone (older
        installs only set the flag on new rows)."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM aircraft_events WHERE type = 'pass' AND in_zone IS NULL AND (latitude IS NOT NULL OR distance_km IS NOT NULL)").fetchone()
        return int(row[0] or 0)

    def count_passes_for_sharing(self, since_ts: float, until_ts: float) -> int:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM aircraft_events WHERE type = 'pass' AND in_zone = 1 AND timestamp >= ? AND timestamp < ?",
                               (float(since_ts), float(until_ts))).fetchone()
        return int(row[0] or 0)

    def get_passes_for_sharing(self, since_ts: float, until_ts: Optional[float] = None) -> List[Dict]:
        """In-zone passes since a time (and before until_ts when given), with the slant
        range at the peak where a sample exists, in the shape the community data service
        takes. Rim passes never leave the station: the same rule as the local statistics."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT e.id, e.timestamp, e.icao24, e.callsign, e.bucket_5m, e.manufacturer, e.model, e.operator,
                       e.route_kind, e.route_airport, e.route_runway, e.route_label,
                       e.spl_db, e.spl_max_db, e.spl_peak_ts, e.distance_km, e.altitude_m,
                       s.ts AS sample_ts, s.slant_km AS sample_slant_km,
                       (SELECT typecode FROM aircraft_registry r WHERE r.icao24 = e.icao24) AS typecode
                FROM aircraft_events e
                LEFT JOIN aircraft_samples s
                  ON s.icao24 = e.icao24 AND e.spl_peak_ts IS NOT NULL
                 AND s.ts BETWEEN e.spl_peak_ts - 20 AND e.spl_peak_ts + 20
                WHERE e.type = 'pass' AND e.in_zone = 1 AND e.timestamp >= ? AND e.timestamp < ?
            ''', (float(since_ts), float(until_ts) if until_ts is not None else 1e12)).fetchall()
        best: Dict[int, Dict] = {}
        for row in rows:
            item = dict(row)
            sample_ts, sample_slant = item.pop('sample_ts'), item.pop('sample_slant_km')
            gap = abs(sample_ts - item['spl_peak_ts']) if sample_ts is not None and item['spl_peak_ts'] else None
            kept = best.get(item['id'])
            if kept is None or (gap is not None and (kept['_gap'] is None or gap < kept['_gap'])):
                item['peak_slant_km'] = sample_slant if gap is not None else None
                item['_gap'] = gap
                best[item['id']] = item
        out = sorted(best.values(), key=lambda r: r['timestamp'])
        for item in out:
            item.pop('_gap', None)
        return out

    def recompute_zone_flags(self, test) -> int:
        """Set in_zone on every row with a position or a distance, from the closest point stored on it.
        Rows from before positions were stored are judged by their distance alone."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT id, latitude, longitude, distance_km FROM aircraft_events WHERE latitude IS NOT NULL OR distance_km IS NOT NULL').fetchall()
            updates = [(1 if test(r['latitude'], r['longitude'], r['distance_km']) else 0, r['id']) for r in rows]
            conn.executemany('UPDATE aircraft_events SET in_zone = ? WHERE id = ?', updates)
            conn.commit()
            return len(updates)

    # ---- aircraft registry ---------------------------------------------------
    def replace_aircraft_registry(self, rows: List[Dict]) -> int:
        """Load the whole registry in one transaction."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('DELETE FROM aircraft_registry')
            conn.executemany('''
                INSERT OR REPLACE INTO aircraft_registry
                    (icao24, registration, typecode, type_name, manufacturer, model, operator, built)
                VALUES (:icao24, :registration, :typecode, :type_name, :manufacturer, :model, :operator, :built)
            ''', rows)
            conn.commit()
            return len(rows)

    def registry_size(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return int(conn.execute('SELECT COUNT(*) FROM aircraft_registry').fetchone()[0] or 0)

    def lookup_aircraft(self, icao24: str) -> Optional[Dict]:
        key = (icao24 or '').strip().lower()
        if not key:
            return None
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT * FROM aircraft_registry WHERE icao24 = ?', (key,)).fetchone()
            return dict(row) if row else None

    def fill_identity_from_registry(self, since_ts: float = 0.0) -> int:
        """Give stored events without a type the registry's make, model, registration and operator."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('''
                UPDATE aircraft_events
                SET manufacturer = COALESCE(NULLIF(TRIM(manufacturer), ''), (SELECT manufacturer FROM aircraft_registry r WHERE r.icao24 = aircraft_events.icao24)),
                    model = COALESCE(NULLIF(TRIM(model), ''), (SELECT model FROM aircraft_registry r WHERE r.icao24 = aircraft_events.icao24)),
                    registration = COALESCE(NULLIF(TRIM(registration), ''), (SELECT registration FROM aircraft_registry r WHERE r.icao24 = aircraft_events.icao24)),
                    operator = COALESCE(NULLIF(TRIM(operator), ''), (SELECT operator FROM aircraft_registry r WHERE r.icao24 = aircraft_events.icao24))
                WHERE timestamp >= ? AND (model IS NULL OR TRIM(model) = '')
                  AND icao24 IN (SELECT icao24 FROM aircraft_registry)
            ''', (float(since_ts),))
            conn.commit()
            return cursor.rowcount

    def replace_airline_designators(self, table: Dict[str, str]) -> int:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS airline_designators (code TEXT PRIMARY KEY, name TEXT)')
            conn.execute('DELETE FROM airline_designators')
            conn.executemany('INSERT INTO airline_designators (code, name) VALUES (?, ?)', list(table.items()))
            conn.commit()
            return len(table)

    def get_airline_designators(self) -> Dict[str, str]:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS airline_designators (code TEXT PRIMARY KEY, name TEXT)')
            return {code: name for code, name in conn.execute('SELECT code, name FROM airline_designators')}

    def fill_operator_from_callsign(self, resolve, since_ts: float = 0.0) -> int:
        """Set the operator where it is empty, from a resolver(callsign) -> name or None."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute('''
                SELECT id, callsign FROM aircraft_events
                WHERE timestamp >= ? AND (operator IS NULL OR TRIM(operator) = '') AND callsign IS NOT NULL
            ''', (float(since_ts),)).fetchall()
            updates = []
            for event_id, callsign in rows:
                name = resolve(callsign)
                if name:
                    updates.append((name, event_id))
            conn.executemany('UPDATE aircraft_events SET operator = ? WHERE id = ?', updates)
            conn.commit()
            return len(updates)

    def get_events_count(self) -> int:
        """Get total count of events in aircraft_events table"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM aircraft_events')
            row = cursor.fetchone()
            return int(row[0] if row and row[0] is not None else 0)

    def sqlite_status(self) -> Dict:
        """Facts read from SQLite itself: query time, integrity, pages, indexes."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            started = time.perf_counter()
            cursor.execute('SELECT COUNT(*) FROM aircraft_events')
            events = int(cursor.fetchone()[0])
            query_ms = int((time.perf_counter() - started) * 1000)

            def pragma(name: str):
                cursor.execute(f'PRAGMA {name}')
                row = cursor.fetchone()
                return row[0] if row else None

            page_count = int(pragma('page_count') or 0)
            freelist = int(pragma('freelist_count') or 0)
            journal_mode = pragma('journal_mode')
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'aircraft_events' ORDER BY name")
            indexes = [row[0] for row in cursor.fetchall()]
            cursor.execute('PRAGMA quick_check')
            check = cursor.fetchone()
            integrity = check[0] if check else 'unknown'
        return {
            'integrity': integrity,
            'events': events,
            'query_ms': query_ms,
            'journal_mode': journal_mode,
            'page_count': page_count,
            'freelist_count': freelist,
            'index_count': len(indexes),
            'indexes': indexes,
            'file_bytes': os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0,
        }

    def clear_recorded_data(self) -> Dict[str, int]:
        """Delete stored passes, visitor stats, and API usage counts. Accounts and settings stay."""
        removed = {}
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            present = {row[0] for row in cursor.fetchall()}
            for table in RECORDED_TABLES:
                if table not in present:
                    continue
                cursor.execute(f'SELECT COUNT(*) FROM {table}')
                removed[table] = int(cursor.fetchone()[0])
                cursor.execute(f'DELETE FROM {table}')
            conn.commit()
        return removed

    def reset_operational_settings(self) -> List[str]:
        """Restore detection, visitor, and zone settings. Accounts and API credentials stay."""
        for key, value in DEFAULT_OPERATIONAL_SETTINGS.items():
            self.set_admin_setting(key, value)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE plane_settings
                SET radius_km = 8.0, alt_max_m = 2000, noise_threshold_db = 50.0,
                    enabled = 1, updated_at = CURRENT_TIMESTAMP
            ''')
            conn.commit()
        return list(DEFAULT_OPERATIONAL_SETTINGS.keys())

    def get_callsign_stats(self, callsign: str) -> Optional[Dict]:
        """Aggregate stats for a given callsign (case-insensitive)."""
        if not callsign:
            return None
        cs = callsign.strip().upper()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # Basic aggregates
            c.execute('''
                SELECT 
                    COUNT(DISTINCT (icao24 || '-' || STRFTIME('%Y-%m-%d', datetime(timestamp, 'unixepoch')))) AS seen,
                    MIN(timestamp) AS first_ts,
                    MAX(timestamp) AS last_ts,
                    MIN(speed_kmh) AS min_speed,
                    MAX(speed_kmh) AS max_speed,
                    AVG(speed_kmh) AS avg_speed,
                    MIN(altitude_m) AS min_alt,
                    MAX(altitude_m) AS max_alt,
                    AVG(altitude_m) AS avg_alt,
                    MIN(spl_db) AS min_noise,
                    MAX(spl_db) AS max_noise,
                    AVG(spl_db) AS avg_noise
                FROM aircraft_events
                WHERE UPPER(COALESCE(callsign, '')) = ?
            ''', (cs,))
            agg = c.fetchone()
            if not agg or agg["seen"] == 0:
                return None

            # Hour-of-day heatmap
            c.execute('''
                SELECT STRFTIME('%H', datetime(timestamp, 'unixepoch')) AS hour,
                       COUNT(DISTINCT icao24) AS cnt
                FROM aircraft_events
                WHERE UPPER(COALESCE(callsign, '')) = ?
                GROUP BY hour
            ''', (cs,))
            by_hour = {row["hour"]: row["cnt"] for row in c.fetchall()}

            # Day-of-week heatmap (0=Sunday per SQLite, map to 0-6)
            c.execute('''
                SELECT STRFTIME('%w', datetime(timestamp, 'unixepoch')) AS wday,
                       COUNT(DISTINCT icao24) AS cnt
                FROM aircraft_events
                WHERE UPPER(COALESCE(callsign, '')) = ?
                GROUP BY wday
            ''', (cs,))
            by_wday = {row["wday"]: row["cnt"] for row in c.fetchall()}

            # The aircraft behind the callsign: newest row first, blanks filled from older rows.
            c.execute('''
                SELECT icao24, registration, manufacturer, model, operator, image_url
                FROM aircraft_events
                WHERE UPPER(COALESCE(callsign, '')) = ?
                ORDER BY timestamp DESC
                LIMIT 50
            ''', (cs,))
            aircraft = {"icao24": None, "registration": None, "manufacturer": None,
                        "model": None, "operator": None, "image_url": None}
            for row in c.fetchall():
                for col in aircraft:
                    if not aircraft[col] and row[col]:
                        aircraft[col] = row[col]

            return {
                "callsign": cs,
                "aircraft": aircraft,
                "seen": int(agg["seen"] or 0),
                "first_ts": float(agg["first_ts"] or 0),
                "last_ts": float(agg["last_ts"] or 0),
                "min_speed_kmh": int(agg["min_speed"] or 0) if agg["min_speed"] is not None else None,
                "max_speed_kmh": int(agg["max_speed"] or 0) if agg["max_speed"] is not None else None,
                "avg_speed_kmh": round(float(agg["avg_speed"]), 1) if agg["avg_speed"] is not None else None,
                "min_altitude_m": int(agg["min_alt"] or 0) if agg["min_alt"] is not None else None,
                "max_altitude_m": int(agg["max_alt"] or 0) if agg["max_alt"] is not None else None,
                "avg_altitude_m": int(round(float(agg["avg_alt"])) ) if agg["avg_alt"] is not None else None,
                "min_noise_db": round(float(agg["min_noise"]), 1) if agg["min_noise"] is not None else None,
                "max_noise_db": round(float(agg["max_noise"]), 1) if agg["max_noise"] is not None else None,
                "avg_noise_db": round(float(agg["avg_noise"]), 1) if agg["avg_noise"] is not None else None,
                "by_hour": by_hour,
                "by_wday": by_wday,
            }

    def get_stats_since(self, since_ts: float) -> Dict:
        """Aggregate stats since given unix timestamp (seconds)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Total events since
            cursor.execute('SELECT COUNT(*) FROM aircraft_events WHERE timestamp >= ?', (since_ts,))
            total_events = int(cursor.fetchone()[0] or 0)

            # Unique aircraft since
            cursor.execute('SELECT COUNT(DISTINCT icao24) FROM aircraft_events WHERE timestamp >= ?', (since_ts,))
            unique_aircraft = int(cursor.fetchone()[0] or 0)

            # Average noise since (exclude nulls)
            cursor.execute('SELECT AVG(spl_db) FROM aircraft_events WHERE timestamp >= ? AND spl_db IS NOT NULL', (since_ts,))
            avg_noise = cursor.fetchone()[0]
            avg_noise = float(avg_noise) if avg_noise is not None else 0.0

            # Closest approach distance (min) since among closest_approach events
            cursor.execute("""
                SELECT MIN(distance_km)
                FROM aircraft_events
                WHERE timestamp >= ? AND type = 'closest_approach' AND distance_km IS NOT NULL
            """, (since_ts,))
            min_distance = cursor.fetchone()[0]
            min_distance = float(min_distance) if min_distance is not None else 0.0

            return {
                "total_events": total_events,
                "unique_aircraft": unique_aircraft,
                "avg_noise_db": avg_noise,
                "closest_approach_km": min_distance,
            }

    def get_planes_heatmap(self, date_str: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, sensor_timezone_offset: int = 2) -> Dict[str, Dict[str, int]]:
        """Return counts (overhead-only) of distinct aircraft per weekday (0-6) and hour (00-23) in sensor timezone.
        Filtering options (exclusive precedence):
          - date_str: limit to that single calendar day (YYYY-MM-DD)
          - start_date & end_date: limit to inclusive range of days (YYYY-MM-DD)
        Args:
            date_str: Single date in YYYY-MM-DD (sensor timezone)
            start_date: Range start date in YYYY-MM-DD (sensor timezone)
            end_date: Range end date in YYYY-MM-DD (sensor timezone)
            sensor_timezone_offset: Sensor timezone offset hours from UTC (e.g., +2)
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            # Build SQLite datetime modifier for hours, e.g., "+2 hours" or "-1 hours"
            sign = '+' if sensor_timezone_offset >= 0 else ''
            offset_str = f"{sign}{int(sensor_timezone_offset)} hours"
            # Overhead-only filter: require (spl_db NOT NULL) OR (distance_km <= inner radius)
            inner_radius_str = self.get_admin_setting('noise_assoc_inner_km', '2.5') or '2.5'
            if date_str:
                c.execute('''
                    SELECT 
                        STRFTIME('%w', datetime(timestamp, 'unixepoch', ?)) AS wday,
                        STRFTIME('%H', datetime(timestamp, 'unixepoch', ?)) AS hour,
                        COUNT(DISTINCT icao24) AS cnt
                    FROM aircraft_events
                    WHERE DATE(datetime(timestamp, 'unixepoch', ?)) = ?
                      AND (spl_db IS NOT NULL OR in_zone = 1)
                    GROUP BY wday, hour
                ''', (offset_str, offset_str, offset_str, date_str))
            elif start_date and end_date:
                # Inclusive range using DATE boundaries in sensor timezone
                c.execute('''
                    SELECT 
                        STRFTIME('%w', datetime(timestamp, 'unixepoch', ?)) AS wday,
                        STRFTIME('%H', datetime(timestamp, 'unixepoch', ?)) AS hour,
                        COUNT(DISTINCT icao24) AS cnt
                    FROM aircraft_events
                    WHERE DATE(datetime(timestamp, 'unixepoch', ?)) >= ?
                      AND DATE(datetime(timestamp, 'unixepoch', ?)) <= ?
                      AND (spl_db IS NOT NULL OR in_zone = 1)
                    GROUP BY wday, hour
                ''', (offset_str, offset_str, offset_str, start_date, offset_str, end_date))
            else:
                c.execute('''
                    SELECT 
                        STRFTIME('%w', datetime(timestamp, 'unixepoch', ?)) AS wday,
                        STRFTIME('%H', datetime(timestamp, 'unixepoch', ?)) AS hour,
                        COUNT(DISTINCT icao24) AS cnt
                    FROM aircraft_events
                    WHERE (spl_db IS NOT NULL OR in_zone = 1)
                    GROUP BY wday, hour
                ''', (offset_str, offset_str))
            result: Dict[str, Dict[str, int]] = {str(d): {} for d in range(7)}
            for row in c.fetchall():
                wday = row['wday']
                hour = row['hour']
                if wday is not None and hour is not None:
                    result[str(wday)][str(hour)] = int(row['cnt'] or 0)
            return result

    def get_planes_heatmap_range(self, start_ts: int, end_ts: int, sensor_timezone_offset: int = 2) -> Dict[str, Dict[str, int]]:
        """Return counts in sensor timezone for events with timestamp in [start_ts, end_ts).
        start_ts/end_ts are UTC epoch seconds.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            sign = '+' if sensor_timezone_offset >= 0 else ''
            offset_str = f"{sign}{int(sensor_timezone_offset)} hours"
            # Same overhead-only rule as the single-day query, so a week equals the sum of its days.
            inner_radius = float(self.get_admin_setting('noise_assoc_inner_km', '2.5') or '2.5')
            c.execute('''
                SELECT 
                    STRFTIME('%w', datetime(timestamp, 'unixepoch', ?)) AS wday,
                    STRFTIME('%H', datetime(timestamp, 'unixepoch', ?)) AS hour,
                    COUNT(DISTINCT icao24) AS cnt
                FROM aircraft_events
                WHERE timestamp >= ? AND timestamp < ?
                  AND (spl_db IS NOT NULL OR in_zone = 1)
                GROUP BY wday, hour
            ''', (offset_str, offset_str, start_ts, end_ts))
            result: Dict[str, Dict[str, int]] = {str(d): {} for d in range(7)}
            for row in c.fetchall():
                wday = row['wday']
                hour = row['hour']
                if wday is not None and hour is not None:
                    result[str(wday)][str(hour)] = int(row['cnt'] or 0)
            return result

    def get_aircraft_events_by_date_hour(self, date_str: str, hour: int, sensor_timezone_offset: int = 2) -> List[Dict]:
        """Get aircraft events for a specific date and hour (overhead-only), deduplicated by aircraft.
        Args:
            date_str: Date in YYYY-MM-DD format (in sensor timezone)
            hour: Hour (0-23) in sensor timezone
            sensor_timezone_offset: Sensor timezone offset in hours from UTC (e.g., +2 for UTC+2, -5 for UTC-5)
        Returns:
            List of unique aircraft events for that date and hour (one per aircraft)
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            # Use sensor timezone for database query
            offset_str = f"{int(sensor_timezone_offset):+d} hours" if sensor_timezone_offset != 0 else "+0 hours"
            inner_radius = float(self.get_admin_setting('noise_assoc_inner_km', '2.5') or '2.5')
            c.execute('''
                SELECT * FROM aircraft_events
                WHERE DATE(datetime(timestamp, 'unixepoch', ?)) = ?
                AND STRFTIME('%H', datetime(timestamp, 'unixepoch', ?)) = ?
                AND icao24 IS NOT NULL
                AND (spl_db IS NOT NULL OR in_zone = 1)
                ORDER BY 
                    CASE 
                        WHEN type = 'closest_approach' THEN 0 
                        WHEN type = 'pass' THEN 1 
                        ELSE 2 
                    END,
                    timestamp DESC
            ''', (offset_str, date_str, offset_str, f"{hour:02d}"))
            
            # One entry per aircraft: the first (best) row, with identity fields
            # filled from its other rows. The pass row learns make, model and
            # operator later than the closest-approach row is written.
            by_aircraft = {}
            unique_events = []
            identity = ('callsign', 'manufacturer', 'model', 'operator', 'registration', 'destination', 'image_url')
            for row in c.fetchall():
                event = dict(row)
                icao24 = event.get('icao24')
                if not icao24:
                    continue
                kept = by_aircraft.get(icao24)
                if kept is None:
                    by_aircraft[icao24] = event
                    unique_events.append(event)
                    continue
                for col in identity:
                    if not kept.get(col) and event.get(col):
                        kept[col] = event[col]
            return unique_events

    # --- User management (CRUD) ---
    def list_users(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute('SELECT id, email, first_name, last_name, is_admin, is_active, created_at, last_login FROM users ORDER BY id')
            return [dict(row) for row in c.fetchall()]

    def get_user(self, user_id: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute('SELECT id, email, first_name, last_name, is_admin, is_active, created_at, last_login FROM users WHERE id = ?', (user_id,))
            row = c.fetchone()
            return dict(row) if row else None

    def get_user_by_email_with_hash(self, email: str) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute('SELECT * FROM users WHERE lower(email) = lower(?)', (email,))
            row = c.fetchone()
            return dict(row) if row else None

    def create_user(self, email: str, password_hash: str, first_name: str = '', last_name: str = '', is_admin: bool = False, is_active: bool = True) -> int:
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('''
                INSERT INTO users (email, first_name, last_name, password_hash, is_admin, is_active)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (email, first_name, last_name, password_hash, 1 if is_admin else 0, 1 if is_active else 0))
            conn.commit()
            return c.lastrowid

    def update_user(self, user_id: int, *, email: Optional[str] = None, first_name: Optional[str] = None,
                    last_name: Optional[str] = None, password_hash: Optional[str] = None,
                    is_admin: Optional[bool] = None, is_active: Optional[bool] = None) -> bool:
        sets = []
        params = []
        if email is not None:
            sets.append('email = ?'); params.append(email)
        if first_name is not None:
            sets.append('first_name = ?'); params.append(first_name)
        if last_name is not None:
            sets.append('last_name = ?'); params.append(last_name)
        if password_hash is not None:
            sets.append('password_hash = ?'); params.append(password_hash)
        if is_admin is not None:
            sets.append('is_admin = ?'); params.append(1 if is_admin else 0)
        if is_active is not None:
            sets.append('is_active = ?'); params.append(1 if is_active else 0)
        if not sets:
            return False
        params.append(user_id)
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()
            return c.rowcount > 0

    def delete_user(self, user_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('DELETE FROM users WHERE id = ?', (user_id,))
            conn.commit()
            return c.rowcount > 0

    def count_users(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM users')
            row = c.fetchone()
            return int(row[0] if row and row[0] is not None else 0)
    
    def get_closest_approaches(self, limit: int = 1000) -> List[Dict]:
        """Get closest approach events for display"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM aircraft_events 
                WHERE type = 'closest_approach' 
                ORDER BY timestamp DESC 
                LIMIT ?
            ''', (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_closest_approach(self, icao24: str) -> Optional[Dict]:
        """Get the closest approach for a specific aircraft"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM aircraft_events
                WHERE icao24 = ? AND type = 'closest_approach'
                ORDER BY distance_km * distance_km
                         + (COALESCE(altitude_m, 0) / 1000.0) * (COALESCE(altitude_m, 0) / 1000.0) ASC
                LIMIT 1
            ''', (icao24,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def log_event(self, event_data: Dict):
        """Log an aircraft event to the database"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            ts = float(event_data.get('ts') or 0)
            bucket_5m = int(ts // 300) if ts else None
            cursor.execute('''
                INSERT INTO aircraft_events 
                (timestamp, type, icao24, callsign, distance_km, altitude_m, 
                 speed_kmh, heading_deg, spl_db, radius_km, alt_max_m, 
                 manufacturer, model, operator, registration, image_url, 
                 latitude, longitude, destination,
                 wx_wind_speed, wx_wind_from_deg, wx_rain,
                 wx_temp_c, wx_temp_2m_c, wx_temp_80m_c,
                 wx_lat, wx_lon, bucket_5m, spl_peak_ts, spl_max_db, in_zone)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(icao24, type, bucket_5m) DO UPDATE SET
                    distance_km = CASE
                        WHEN excluded.distance_km IS NOT NULL AND (aircraft_events.distance_km IS NULL
                          OR excluded.distance_km * excluded.distance_km
                             + (COALESCE(excluded.altitude_m, 0) / 1000.0) * (COALESCE(excluded.altitude_m, 0) / 1000.0)
                           < aircraft_events.distance_km * aircraft_events.distance_km
                             + (COALESCE(aircraft_events.altitude_m, 0) / 1000.0) * (COALESCE(aircraft_events.altitude_m, 0) / 1000.0))
                        THEN excluded.distance_km ELSE aircraft_events.distance_km END,
                    spl_db = CASE
                        WHEN excluded.spl_db IS NOT NULL AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                        THEN excluded.spl_db ELSE aircraft_events.spl_db END,
                    altitude_m = CASE
                        WHEN excluded.distance_km IS NOT NULL AND (aircraft_events.distance_km IS NULL
                          OR excluded.distance_km * excluded.distance_km
                             + (COALESCE(excluded.altitude_m, 0) / 1000.0) * (COALESCE(excluded.altitude_m, 0) / 1000.0)
                           < aircraft_events.distance_km * aircraft_events.distance_km
                             + (COALESCE(aircraft_events.altitude_m, 0) / 1000.0) * (COALESCE(aircraft_events.altitude_m, 0) / 1000.0))
                        THEN excluded.altitude_m ELSE aircraft_events.altitude_m END,
                    speed_kmh = COALESCE(excluded.speed_kmh, aircraft_events.speed_kmh),
                    heading_deg = COALESCE(excluded.heading_deg, aircraft_events.heading_deg),
                    callsign = CASE WHEN TRIM(COALESCE(excluded.callsign, '')) <> '' THEN excluded.callsign ELSE aircraft_events.callsign END,
                    spl_peak_ts = CASE
                        WHEN excluded.spl_db IS NOT NULL AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                        THEN excluded.spl_peak_ts ELSE aircraft_events.spl_peak_ts END,
                    spl_max_db = CASE
                        WHEN excluded.spl_db IS NOT NULL AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                        THEN excluded.spl_max_db ELSE aircraft_events.spl_max_db END,
                    in_zone = CASE WHEN excluded.in_zone = 1 THEN 1 ELSE COALESCE(aircraft_events.in_zone, excluded.in_zone) END,
                    radius_km = COALESCE(excluded.radius_km, aircraft_events.radius_km),
                    alt_max_m = COALESCE(excluded.alt_max_m, aircraft_events.alt_max_m),
                    manufacturer = COALESCE(excluded.manufacturer, aircraft_events.manufacturer),
                    model = COALESCE(excluded.model, aircraft_events.model),
                    operator = COALESCE(excluded.operator, aircraft_events.operator),
                    registration = COALESCE(excluded.registration, aircraft_events.registration),
                    image_url = COALESCE(excluded.image_url, aircraft_events.image_url),
                    latitude = COALESCE(excluded.latitude, aircraft_events.latitude),
                    longitude = COALESCE(excluded.longitude, aircraft_events.longitude),
                    destination = COALESCE(excluded.destination, aircraft_events.destination),
                    wx_wind_speed = CASE
                        WHEN excluded.spl_db IS NOT NULL
                         AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                         AND excluded.wx_lat IS NOT NULL AND excluded.wx_lon IS NOT NULL
                        THEN excluded.wx_wind_speed ELSE aircraft_events.wx_wind_speed END,
                    wx_wind_from_deg = CASE
                        WHEN excluded.spl_db IS NOT NULL
                         AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                         AND excluded.wx_lat IS NOT NULL AND excluded.wx_lon IS NOT NULL
                        THEN excluded.wx_wind_from_deg ELSE aircraft_events.wx_wind_from_deg END,
                    wx_rain = CASE
                        WHEN excluded.spl_db IS NOT NULL
                         AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                         AND excluded.wx_lat IS NOT NULL AND excluded.wx_lon IS NOT NULL
                        THEN excluded.wx_rain ELSE aircraft_events.wx_rain END,
                    wx_temp_c = CASE
                        WHEN excluded.spl_db IS NOT NULL
                         AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                         AND excluded.wx_lat IS NOT NULL AND excluded.wx_lon IS NOT NULL
                        THEN excluded.wx_temp_c ELSE aircraft_events.wx_temp_c END,
                    wx_temp_2m_c = CASE
                        WHEN excluded.spl_db IS NOT NULL
                         AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                         AND excluded.wx_lat IS NOT NULL AND excluded.wx_lon IS NOT NULL
                        THEN excluded.wx_temp_2m_c ELSE aircraft_events.wx_temp_2m_c END,
                    wx_temp_80m_c = CASE
                        WHEN excluded.spl_db IS NOT NULL
                         AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                         AND excluded.wx_lat IS NOT NULL AND excluded.wx_lon IS NOT NULL
                        THEN excluded.wx_temp_80m_c ELSE aircraft_events.wx_temp_80m_c END,
                    wx_lat = CASE
                        WHEN excluded.spl_db IS NOT NULL
                         AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                         AND excluded.wx_lat IS NOT NULL AND excluded.wx_lon IS NOT NULL
                        THEN excluded.wx_lat ELSE aircraft_events.wx_lat END,
                    wx_lon = CASE
                        WHEN excluded.spl_db IS NOT NULL
                         AND (aircraft_events.spl_db IS NULL OR excluded.spl_db > aircraft_events.spl_db)
                         AND excluded.wx_lat IS NOT NULL AND excluded.wx_lon IS NOT NULL
                        THEN excluded.wx_lon ELSE aircraft_events.wx_lon END,
                    timestamp = MAX(aircraft_events.timestamp, excluded.timestamp)
            ''', (
                ts,
                event_data.get('type'),
                event_data.get('icao24'),
                event_data.get('callsign'),
                event_data.get('distance_km'),
                event_data.get('altitude_m'),
                event_data.get('speed_kmh'),
                event_data.get('heading_deg'),
                event_data.get('spl_db'),
                event_data.get('radius_km'),
                event_data.get('alt_max_m'),
                event_data.get('manufacturer'),
                event_data.get('model'),
                event_data.get('operator'),
                event_data.get('registration'),
                event_data.get('image_url'),
                event_data.get('latitude'),
                event_data.get('longitude'),
                event_data.get('destination'),
                event_data.get('wx_wind_speed'),
                event_data.get('wx_wind_from_deg'),
                event_data.get('wx_rain'),
                event_data.get('wx_temp_c'),
                event_data.get('wx_temp_2m_c'),
                event_data.get('wx_temp_80m_c'),
                event_data.get('wx_lat'),
                event_data.get('wx_lon'),
                bucket_5m,
                event_data.get('spl_peak_ts'),
                event_data.get('spl_max_db'),
                1 if event_data.get('in_zone') else (0 if event_data.get('in_zone') is not None else None),
            ))
            conn.commit()
    
    def cleanup_old_events(self, days_to_keep: int = 30):
        """Clean up old events to keep database size manageable"""
        cutoff_time = datetime.now() - timedelta(days=days_to_keep)
        cutoff_timestamp = cutoff_time.timestamp()
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM aircraft_events 
                WHERE timestamp < ?
            ''', (cutoff_timestamp,))
            deleted_count = cursor.rowcount
            conn.commit()
            logging.info(f"Cleaned up {deleted_count} old events")
            return deleted_count
    
    def get_admin_setting(self, key: str, default: str = None) -> str:
        """Get an admin setting value"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM admin_settings WHERE key = ?', (key,))
            result = cursor.fetchone()
            return result[0] if result else default
    
    def set_admin_setting(self, key: str, value: str) -> bool:
        """Set an admin setting value"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO admin_settings (key, value, updated_at) 
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                ''', (key, value))
                conn.commit()
                return True
        except Exception as e:
            logging.error(f"Failed to set admin setting {key}: {e}")
            return False
    
    # -- visitor statistics --------------------------------------------------

    def save_stats_day(self, day: str, views: int, api_views: int, visitors: int, hours: Dict, dims: Dict):
        """Replace one day's totals, hours and per-kind counts in one transaction."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''INSERT INTO stats_days (day, views, api_views, visitors, updated_at) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(day) DO UPDATE SET views = excluded.views, api_views = excluded.api_views,
                            visitors = excluded.visitors, updated_at = excluded.updated_at''',
                         (day, int(views), int(api_views), int(visitors), time.time()))
            conn.executemany('''INSERT INTO stats_hours (hour_ts, day, views, visitors) VALUES (?, ?, ?, ?)
                                ON CONFLICT(hour_ts) DO UPDATE SET views = excluded.views, visitors = excluded.visitors''',
                             [(int(ts), day, int(v[0]), int(v[1])) for ts, v in hours.items()])
            for kind, counter in dims.items():
                conn.execute("DELETE FROM stats_dims WHERE day = ? AND kind = ?", (day, kind))
                conn.executemany("INSERT INTO stats_dims (day, kind, key, n) VALUES (?, ?, ?, ?)",
                                 [(day, kind, str(key)[:80], int(n)) for key, n in counter.items()])

    def prune_stats(self, keep_days: int):
        cutoff = (datetime.now() - timedelta(days=int(keep_days))).strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM stats_hours WHERE day < ?", (cutoff,))
            conn.execute("DELETE FROM stats_dims WHERE day < ?", (cutoff,))

    def get_stats_days(self, days: int) -> List[Dict]:
        cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT day, views, api_views, visitors FROM stats_days WHERE day >= ? ORDER BY day", (cutoff,)).fetchall()]

    def get_stats_hours(self, days: int) -> List[Dict]:
        since = time.time() - int(days) * 86400
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT hour_ts, views, visitors FROM stats_hours WHERE hour_ts >= ? ORDER BY hour_ts", (since,)).fetchall()]

    def get_stats_dims(self, days: int, kind: str, limit: int = 15) -> List[Dict]:
        cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                '''SELECT key, SUM(n) AS n FROM stats_dims WHERE day >= ? AND kind = ? GROUP BY key
                   ORDER BY n DESC, key LIMIT ?''', (cutoff, kind, int(limit))).fetchall()]

    def get_passes_per_hour(self, days: int, loud_db: float = 60.0) -> Dict[int, tuple]:
        """{hour start: (passes, passes at or above loud_db)} for in-zone passes."""
        since = time.time() - int(days) * 86400
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute('''SELECT CAST(timestamp / 3600 AS INTEGER) * 3600 AS h, COUNT(*),
                                       SUM(CASE WHEN spl_db >= ? THEN 1 ELSE 0 END)
                                   FROM aircraft_events WHERE type = 'pass' AND in_zone = 1 AND timestamp >= ? GROUP BY h''',
                                (float(loud_db), since)).fetchall()
        return {int(r[0]): (int(r[1]), int(r[2] or 0)) for r in rows}

    def stats_table_sizes(self) -> Dict[str, int]:
        """Row counts of the statistics tables and of the old visitor tables."""
        out = {}
        with sqlite3.connect(self.db_path) as conn:
            for table in ("stats_days", "stats_hours", "stats_dims", "guard_bans", "visitor_sessions", "page_views", "visitor_interactions", "daily_analytics"):
                try:
                    out[table] = conn.execute("SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0]
                except sqlite3.OperationalError:
                    out[table] = 0
        return out

    def drop_legacy_visitor_tables(self):
        """The old tracker's tables, with raw addresses in them. Only on request."""
        with sqlite3.connect(self.db_path) as conn:
            for table in ("visitor_interactions", "page_views", "visitor_sessions", "daily_analytics"):
                conn.execute("DROP TABLE IF EXISTS {0}".format(table))
            conn.execute("VACUUM")

    # -- scraper guard -------------------------------------------------------

    def save_ban(self, ip: str, until_ts: float, reason: str, bans: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''INSERT INTO guard_bans (ip, until_ts, reason, bans) VALUES (?, ?, ?, ?)
                            ON CONFLICT(ip) DO UPDATE SET until_ts = excluded.until_ts, reason = excluded.reason, bans = excluded.bans''',
                         (ip, float(until_ts), reason[:80], int(bans)))

    def load_bans(self):
        """Active bans as (ip, until_ts, bans); old rows are dropped on the way."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM guard_bans WHERE until_ts < ?", (time.time() - 7 * 86400,))
            return [(row[0], row[1], row[2]) for row in conn.execute(
                "SELECT ip, until_ts, bans FROM guard_bans WHERE until_ts > ?", (time.time(),)).fetchall()]

    def drop_ban(self, ip: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM guard_bans WHERE ip = ?", (ip,))

    def get_all_admin_settings(self) -> Dict[str, str]:
        """Get all admin settings"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM admin_settings')
            return dict(cursor.fetchall())
    
    def update_aircraft_speed_heading(self, icao24: str, speed_kmh: int = None, heading_deg: int = None) -> bool:
        """Update speed and heading for a specific aircraft in the most recent record"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Update the most recent record for this aircraft
                if speed_kmh is not None and heading_deg is not None:
                    cursor.execute('''
                        UPDATE aircraft_events 
                        SET speed_kmh = ?, heading_deg = ?
                        WHERE icao24 = ? AND id = (
                            SELECT id FROM aircraft_events 
                            WHERE icao24 = ? 
                            ORDER BY timestamp DESC 
                            LIMIT 1
                        )
                    ''', (speed_kmh, heading_deg, icao24, icao24))
                elif speed_kmh is not None:
                    cursor.execute('''
                        UPDATE aircraft_events 
                        SET speed_kmh = ?
                        WHERE icao24 = ? AND id = (
                            SELECT id FROM aircraft_events 
                            WHERE icao24 = ? 
                            ORDER BY timestamp DESC 
                            LIMIT 1
                        )
                    ''', (speed_kmh, icao24, icao24))
                elif heading_deg is not None:
                    cursor.execute('''
                        UPDATE aircraft_events 
                        SET heading_deg = ?
                        WHERE icao24 = ? AND id = (
                            SELECT id FROM aircraft_events 
                            WHERE icao24 = ? 
                            ORDER BY timestamp DESC 
                            LIMIT 1
                        )
                    ''', (heading_deg, icao24, icao24))
                
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logging.error(f"Failed to update aircraft speed/heading for {icao24}: {e}")
            return False

    # Visitor tracking methods
    def create_or_update_session(self, session_id, ip_hash, user_agent_hash, device_type, browser, operating_system, referrer, page_path, referrer_type=None, search_engine=None, search_query=None, country=None, is_bot=False, bot_type=None):
        """Create or update a visitor session"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Check if session exists
            cursor.execute('SELECT id FROM visitor_sessions WHERE session_id = ?', (session_id,))
            existing = cursor.fetchone()
            
            if existing:
                # Update existing session
                cursor.execute('''
                    UPDATE visitor_sessions 
                    SET last_visit = CURRENT_TIMESTAMP, 
                        page_views = page_views + 1,
                        is_bounce = FALSE,
                        session_duration = (julianday(CURRENT_TIMESTAMP) - julianday(first_visit)) * 86400
                    WHERE session_id = ?
                ''', (session_id,))
            else:
                # Create new session
                cursor.execute('''
                    INSERT INTO visitor_sessions 
                    (session_id, ip_hash, user_agent_hash, device_type, browser, operating_system, referrer, referrer_type, search_engine, search_query, country, is_bot, bot_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (session_id, ip_hash, user_agent_hash, device_type, browser, operating_system, referrer, referrer_type, search_engine, search_query, country, is_bot, bot_type))
            
            conn.commit()

    def track_pageview(self, session_id, page_path, load_time, referrer, utm_source, utm_medium, utm_campaign):
        """Track a page view"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO page_views 
                (session_id, page_path, load_time, referrer, utm_source, utm_medium, utm_campaign)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (session_id, page_path, load_time, referrer, utm_source, utm_medium, utm_campaign))
            conn.commit()

    def track_interaction(self, session_id, interaction_type, interaction_data, timestamp=None):
        """Track a visitor interaction"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO visitor_interactions 
                (session_id, interaction_type, interaction_data, timestamp)
                VALUES (?, ?, ?, ?)
            ''', (session_id, interaction_type, json.dumps(interaction_data), timestamp or datetime.now().isoformat()))
            conn.commit()

    def update_session_duration(self, session_id, duration):
        """Update session duration"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE visitor_sessions 
                SET session_duration = ?
                WHERE session_id = ?
            ''', (duration, session_id))
            conn.commit()

    def get_analytics_overview(self, days=30):
        """Get analytics overview for the specified number of days"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Get basic stats
            cursor.execute('''
                SELECT 
                    COUNT(DISTINCT session_id) as total_sessions,
                    COUNT(DISTINCT ip_hash) as unique_visitors,
                    SUM(page_views) as total_page_views,
                    AVG(session_duration) as avg_session_duration,
                    SUM(CASE WHEN is_bounce THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as bounce_rate
                FROM visitor_sessions 
                WHERE first_visit >= datetime('now', '-{} days')
                AND is_bot = FALSE
            '''.format(days))
            
            result = cursor.fetchone()
            return {
                'total_sessions': result[0] or 0,
                'unique_visitors': result[1] or 0,
                'total_page_views': result[2] or 0,
                'avg_session_duration': result[3] or 0,
                'bounce_rate': result[4] or 0
            }

    def get_traffic_sources(self, days=30):
        """Get traffic source breakdown"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    referrer_type,
                    search_engine,
                    COUNT(*) as count
                FROM visitor_sessions 
                WHERE first_visit >= datetime('now', '-{} days')
                AND is_bot = FALSE
                GROUP BY referrer_type, search_engine
                ORDER BY count DESC
            '''.format(days))
            
            results = cursor.fetchall()
            sources = []
            for row in results:
                sources.append({
                    'referrer_type': row[0],
                    'search_engine': row[1],
                    'social_network': None,
                    'count': row[2]
                })
            return sources

    def get_bot_statistics(self, days=30):
        """Get bot detection statistics"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Total sessions
            cursor.execute('''
                SELECT COUNT(*) as total_sessions
                FROM visitor_sessions 
                WHERE first_visit >= datetime('now', '-{} days')
            '''.format(days))
            total_sessions = cursor.fetchone()[0]
            
            # Bot sessions
            cursor.execute('''
                SELECT COUNT(*) as bot_sessions
                FROM visitor_sessions 
                WHERE first_visit >= datetime('now', '-{} days')
                AND is_bot = TRUE
            '''.format(days))
            bot_sessions = cursor.fetchone()[0]
            
            # Real visitors
            real_visitors = total_sessions - bot_sessions
            
            # Bot type breakdown
            cursor.execute('''
                SELECT bot_type, COUNT(*) as count
                FROM visitor_sessions 
                WHERE first_visit >= datetime('now', '-{} days')
                AND is_bot = TRUE
                GROUP BY bot_type
                ORDER BY count DESC
            '''.format(days))
            bot_breakdown = cursor.fetchall()
            
            return {
                'total_sessions': total_sessions,
                'real_visitors': real_visitors,
                'bots_filtered': bot_sessions,
                'bot_percentage': round((bot_sessions / total_sessions) * 100, 1) if total_sessions > 0 else 0,
                'bot_breakdown': [{'type': row[0], 'count': row[1]} for row in bot_breakdown]
            }

    def get_search_engine_stats(self, days=30):
        """Get search engine statistics"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    search_engine,
                    COUNT(*) as count,
                    COUNT(DISTINCT search_query) as unique_queries
                FROM visitor_sessions 
                WHERE first_visit >= datetime('now', '-{} days')
                AND is_bot = FALSE
                AND referrer_type = 'search_engine'
                AND search_engine IS NOT NULL
                GROUP BY search_engine
                ORDER BY count DESC
            '''.format(days))
            return cursor.fetchall()

    def get_top_referrers(self, days=30, limit=10):
        """Get top referring domains"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    CASE 
                        WHEN referrer IS NULL THEN 'Direct'
                        ELSE substr(referrer, 8, instr(substr(referrer, 8), '/') - 1)
                    END as domain,
                    COUNT(*) as count
                FROM visitor_sessions 
                WHERE first_visit >= datetime('now', '-{} days')
                AND is_bot = FALSE
                GROUP BY domain
                ORDER BY count DESC
                LIMIT ?
            '''.format(days), (limit,))
            return cursor.fetchall()

    def get_search_queries(self, days=30, limit=20):
        """Get top search queries"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    search_query,
                    search_engine,
                    COUNT(*) as count
                FROM visitor_sessions 
                WHERE first_visit >= datetime('now', '-{} days')
                AND is_bot = FALSE
                AND search_query IS NOT NULL
                AND search_query != ''
                GROUP BY search_query, search_engine
                ORDER BY count DESC
                LIMIT ?
            '''.format(days), (limit,))
            return cursor.fetchall()

    def get_realtime_stats(self):
        """Get real-time visitor statistics"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Active users (last 5 minutes)
            cursor.execute('''
                SELECT COUNT(DISTINCT session_id) as active_users
                FROM visitor_sessions 
                WHERE last_visit >= datetime('now', '-5 minutes')
                AND is_bot = FALSE
            ''')
            active_users = cursor.fetchone()[0]
            
            # Today's visitors
            cursor.execute('''
                SELECT COUNT(DISTINCT ip_hash) as today_visitors
                FROM visitor_sessions 
                WHERE DATE(first_visit) = DATE('now')
                AND is_bot = FALSE
            ''')
            today_visitors = cursor.fetchone()[0]
            
            # Peak hour (simplified - just return current hour for now)
            peak_hour = datetime.now().strftime('%H:00')
            
            return {
                'active_users': active_users or 0,
                'today_visitors': today_visitors or 0,
                'peak_hour': peak_hour
            }

    def track_api_usage(self, api_name: str, daily_requests: int = 1, daily_limit: int = None, 
                       remaining_requests: int = None, last_reset_timestamp: int = None,
                       rate_limit_per_minute: int = None, pricing: str = None, reset_type: str = 'daily'):
        """Track API usage for a specific day"""
        from datetime import date
        today = date.today().isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Check if record exists for today
            cursor.execute('''
                SELECT daily_requests FROM api_usage_daily 
                WHERE api_name = ? AND date = ?
            ''', (api_name, today))
            
            result = cursor.fetchone()
            
            if result:
                # Update existing record
                new_count = result[0] + daily_requests
                cursor.execute('''
                    UPDATE api_usage_daily 
                    SET daily_requests = ?, 
                        daily_limit = COALESCE(?, daily_limit),
                        remaining_requests = COALESCE(?, remaining_requests),
                        last_reset_timestamp = COALESCE(?, last_reset_timestamp),
                        rate_limit_per_minute = COALESCE(?, rate_limit_per_minute),
                        pricing = COALESCE(?, pricing),
                        reset_type = COALESCE(?, reset_type),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE api_name = ? AND date = ?
                ''', (new_count, daily_limit, remaining_requests, last_reset_timestamp,
                      rate_limit_per_minute, pricing, reset_type, api_name, today))
            else:
                # Create new record
                cursor.execute('''
                    INSERT INTO api_usage_daily 
                    (api_name, date, daily_requests, daily_limit, remaining_requests, 
                     last_reset_timestamp, rate_limit_per_minute, pricing, reset_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (api_name, today, daily_requests, daily_limit, remaining_requests,
                      last_reset_timestamp, rate_limit_per_minute, pricing, reset_type))
            
            conn.commit()

    def get_api_usage_stats(self):
        """Get current API usage statistics for all APIs"""
        from datetime import date
        today = date.today().isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Get today's usage for all APIs
            cursor.execute('''
                SELECT api_name, daily_requests, daily_limit, remaining_requests,
                       last_reset_timestamp, rate_limit_per_minute, pricing, reset_type
                FROM api_usage_daily 
                WHERE date = ?
                ORDER BY api_name
            ''', (today,))
            
            results = cursor.fetchall()
            
            # Convert to dictionary format expected by the frontend
            api_stats = {}
            for row in results:
                api_stats[row['api_name']] = {
                    'name': row['api_name'].replace('_', ' ').title(),
                    'daily_requests': row['daily_requests'] or 0,
                    'daily_limit': row['daily_limit'],
                    'remaining_requests': row['remaining_requests'],
                    'last_reset': row['last_reset_timestamp'],
                    'rate_limit_per_minute': row['rate_limit_per_minute'] or 30,
                    'pricing': row['pricing'] or 'Unknown',
                    'reset_type': row['reset_type'] or 'daily'
                }
            
            return api_stats

    def cleanup_old_api_usage(self, days_to_keep: int = 30):
        """Clean up old API usage records to keep database size manageable"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM api_usage_daily 
                WHERE date < date('now', '-{} days')
            '''.format(days_to_keep))
            deleted_count = cursor.rowcount
            conn.commit()
            if deleted_count > 0:
                logging.info(f"Cleaned up {deleted_count} old API usage records")

# Global database instance
# PN_DB_PATH moves the database out of the code directory, e.g. /app/data/plane_noise.db in a container.
db = PlaneNoiseDB(os.getenv("PN_DB_PATH", "plane_noise.db"))

if __name__ == "__main__":
    # Test the database
    print("Testing database...")
    print(f"Admin setting radius: {db.get_admin_setting('detection_radius_km')}")
    print(f"Recent events: {len(db.get_recent_events(10))}")
    print("Database test complete!")
