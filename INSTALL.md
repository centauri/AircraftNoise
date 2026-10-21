# Aircraft Noise — Install & Production Guide

This guide covers a Debian or Ubuntu host. The Flask app is `proxy_server.py`, the database SQLite, the admin pages under `/admin`; the recorder polls the aircraft feed and the noise sensor in the background.

## 1) Prerequisites
- Python 3.11 or newer
- pip, virtualenv (recommended)
- Optional for production: Nginx (or Caddy), Certbot (Let’s Encrypt), systemd

## 2) Setup (development)
```bash
cd plane_noise
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python proxy_server.py
# Visit http://127.0.0.1:8787
```
Notes:
- The backend starts a background polling thread which fetches aircraft/noise data and writes events to SQLite.
- Configure providers and polling intervals in Admin → Settings (per‑API).

## 3) Production (Gunicorn + Nginx + TLS)

### 3.1 Run with Gunicorn
- We added `deploy/gunicorn.conf.py` and `deploy/run_gunicorn.sh`.
```bash
./deploy/run_gunicorn.sh
# or: gunicorn -w 3 -b 127.0.0.1:8787 proxy_server:app
```
Env options:
- `GUNICORN_BIND`, `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `GUNICORN_LOGLEVEL`
- `FLASK_SESSION_SECURE=true` to mark session cookies Secure under HTTPS

### 3.2 Reverse proxy (Nginx)
- Use `deploy/nginx.conf` as a template. Replace `example.com` and certificate paths.
- Key items:
  - HTTP→HTTPS redirect
  - TLS 1.2/1.3, HSTS
  - Security headers (XCTO, XFO, Referrer-Policy)
  - Proxy to `127.0.0.1:8787`
  - Optionally serve `/static/` directly

### 3.3 TLS
- Install Certbot and issue certificates for your domain; reload Nginx.

### 3.4 systemd service (example)
Create `/etc/systemd/system/plane-noise.service`:
```
[Unit]
Description=Aircraft Noise (Gunicorn)
After=network.target

[Service]
WorkingDirectory=/opt/aircraft-noise
Environment=FLASK_SESSION_SECURE=true
ExecStart=/usr/bin/env bash deploy/run_gunicorn.sh
Restart=always
User=www-data
Group=www-data

[Install]
WantedBy=multi-user.target
```
Enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now plane-noise
```

## 4) Configuration
- All API settings (rate limit, cache timeout, poll interval, credentials) are managed via Admin → Settings.
- The backend reads per‑API `poll_interval_seconds` and honors provider rate limits; no frontend polling of external APIs.
- Session cookies: `SESSION_COOKIE_HTTPONLY=true` (set). To mark Secure under TLS set env `FLASK_SESSION_SECURE=true`.

## 5) Database
- Default SQLite DB file is `plane_noise.db` in project root.
- For durability on single‑host: enable WAL mode and back up regularly.
- Stats endpoints: `/admin/stats`, recent events `/recent_events`.

## 6) Backups & Logs
- Back up `plane_noise.db` and (optionally) `/var/log/nginx/*.log`.
- Gunicorn logs go to stdout/stderr by default (see gunicorn.conf).

## 7) Security hardening checklist
- HTTPS with HSTS; redirect HTTP→HTTPS
- Strong Flask secret key; run behind reverse proxy
- Admin password policy; consider IP allowlist for `/admin/*`
- Set `FLASK_SESSION_SECURE=true` in production
- CSP at proxy if you want stricter embedding
- Rate‑limit admin login at the proxy (e.g., Nginx `limit_req`)

## 8) Operations
- Health: `/health`
- Noise debug: `/api/noise/recent`, `/debug/noise`
- Polling runs in a background thread; intervals configured per API

## 9) Upgrades
- Pull code, restart the systemd service
- If DB schema changes, the app will manage compatible reads; back up DB before major upgrades

## 10) Troubleshooting
- Seeing 429 from providers: increase poll intervals or adjust per‑API rate limits in Admin → Settings
- No new aircraft logged: check provider auth and background poller status in logs
- Frontend duplicates: the table dedupes by `icao24`; stale cache can be cleared with hard refresh
