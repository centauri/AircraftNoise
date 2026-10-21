"""
Configuration file for the Aircraft Noise application.
"""

import os
from datetime import timedelta

class Config:
    """Base configuration"""
    
    # Security
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'dev-jwt-secret-change-in-production')
    
    # Database
    DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///plane_noise.db')
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # JWT Configuration
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(seconds=int(os.getenv('JWT_ACCESS_TOKEN_EXPIRES', 3600)))
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(seconds=int(os.getenv('JWT_REFRESH_TOKEN_EXPIRES', 2592000)))
    
    # Email Configuration
    MAIL_SERVER = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT = int(os.getenv('MAIL_PORT', 587))
    MAIL_USE_TLS = os.getenv('MAIL_USE_TLS', 'True').lower() == 'true'
    MAIL_USERNAME = os.getenv('MAIL_USERNAME')
    MAIL_PASSWORD = os.getenv('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.getenv('MAIL_DEFAULT_SENDER')
    
    # Security Headers
    FORCE_HTTPS = os.getenv('FORCE_HTTPS', 'True').lower() == 'true'
    STRICT_TRANSPORT_SECURITY = os.getenv('STRICT_TRANSPORT_SECURITY', 'True').lower() == 'true'
    CONTENT_SECURITY_POLICY = os.getenv('CONTENT_SECURITY_POLICY', "default-src 'self'")
    
    # CORS
    ALLOWED_ORIGINS = os.getenv('ALLOWED_ORIGINS', 'http://localhost:3000').split(',')
    
    # Rate Limiting
    RATELIMIT_STORAGE_URL = os.getenv('RATELIMIT_STORAGE_URL', 'memory://')
    
    # OpenSky API
    OS_USERNAME = os.getenv('OS_USERNAME')
    OS_PASSWORD = os.getenv('OS_PASSWORD')
    OS_CLIENT_ID = os.getenv('OS_CLIENT_ID')
    OS_CLIENT_SECRET = os.getenv('OS_CLIENT_SECRET')
    
    # Sensor Configuration
    SENSOR_ID = os.getenv('SENSOR_ID', '69616')
    SENSOR_LAT = float(os.getenv('SENSOR_LAT', '0'))
    SENSOR_LON = float(os.getenv('SENSOR_LON', '0'))
    SENSOR_TIMEZONE = os.getenv('SENSOR_TIMEZONE', 'Europe/Amsterdam')  # Netherlands timezone with automatic DST
    
    # Application
    HOST = os.getenv('HOST', '0.0.0.0')
    PORT = int(os.getenv('PORT', 8787))
    DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    
    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    
    # Monitoring
    SENTRY_DSN = os.getenv('SENTRY_DSN')

class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    FORCE_HTTPS = False
    STRICT_TRANSPORT_SECURITY = False

class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    FORCE_HTTPS = True
    STRICT_TRANSPORT_SECURITY = True

class TestingConfig(Config):
    """Testing configuration"""
    TESTING = True
    DATABASE_URL = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False

# Configuration mapping
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
