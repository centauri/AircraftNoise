#!/usr/bin/env python3
"""
TLS Configuration and Certificate Management
Handles both self-signed certificates for development and Let's Encrypt for production.
"""

import os
import ssl
import logging
import subprocess
import tempfile
import ipaddress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
import requests

logger = logging.getLogger(__name__)

class TLSConfig:
    """TLS configuration manager for both development and production"""
    
    def __init__(self, config_dir: str = "tls_certs"):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(exist_ok=True)
        
        # Certificate paths
        self.cert_file = self.config_dir / "server.crt"
        self.key_file = self.config_dir / "server.key"
        self.letsencrypt_dir = self.config_dir / "letsencrypt"
        self.letsencrypt_dir.mkdir(exist_ok=True)
        
        # Configuration
        self.domain = os.getenv("TLS_DOMAIN", "localhost")
        self.email = os.getenv("TLS_EMAIL", "admin@localhost")
        self.staging = os.getenv("LETSENCRYPT_STAGING", "false").lower() == "true"
        self.force_https = os.getenv("FORCE_HTTPS", "true").lower() == "true"
        
    def get_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Get SSL context for the server"""
        try:
            if self._has_valid_certificates():
                context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                context.load_cert_chain(str(self.cert_file), str(self.key_file))
                context.set_ciphers('ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!MD5:!DSS')
                context.options |= ssl.OP_NO_SSLv2
                context.options |= ssl.OP_NO_SSLv3
                context.options |= ssl.OP_NO_TLSv1
                context.options |= ssl.OP_NO_TLSv1_1
                return context
            else:
                logger.warning("No valid certificates found, running without TLS")
                return None
        except Exception as e:
            logger.error(f"Failed to create SSL context: {e}")
            return None
    
    def _has_valid_certificates(self) -> bool:
        """Check if valid certificates exist"""
        if not self.cert_file.exists() or not self.key_file.exists():
            return False
        
        try:
            # Check if certificate is not expired
            with open(self.cert_file, 'rb') as f:
                cert_data = f.read()
            cert = x509.load_pem_x509_certificate(cert_data)
            return cert.not_valid_after > datetime.utcnow()
        except Exception:
            return False
    
    def generate_self_signed_certificate(self) -> bool:
        """Generate self-signed certificate for development"""
        try:
            logger.info("Generating self-signed certificate for development...")
            
            # Generate private key
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
            
            # Create certificate
            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
                x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Development"),
                x509.NameAttribute(NameOID.LOCALITY_NAME, "Local"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Aircraft Noise"),
                x509.NameAttribute(NameOID.COMMON_NAME, self.domain),
            ])
            
            cert = x509.CertificateBuilder().subject_name(
                subject
            ).issuer_name(
                issuer
            ).public_key(
                private_key.public_key()
            ).serial_number(
                x509.random_serial_number()
            ).not_valid_before(
                datetime.utcnow()
            ).not_valid_after(
                datetime.utcnow() + timedelta(days=365)
            ).add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName(self.domain),
                    x509.DNSName("localhost"),
                    x509.DNSName("127.0.0.1"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]),
                critical=False,
            ).sign(private_key, hashes.SHA256())
            
            # Write private key
            with open(self.key_file, "wb") as f:
                f.write(private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                ))
            
            # Write certificate
            with open(self.cert_file, "wb") as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM))
            
            logger.info(f"Self-signed certificate generated: {self.cert_file}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to generate self-signed certificate: {e}")
            return False
    
    def setup_letsencrypt_certificate(self) -> bool:
        """Setup Let's Encrypt certificate using certbot"""
        try:
            if self.domain == "localhost":
                logger.warning("Cannot use Let's Encrypt with localhost, falling back to self-signed")
                return self.generate_self_signed_certificate()
            
            logger.info(f"Setting up Let's Encrypt certificate for {self.domain}")
            
            # Check if certbot is available
            try:
                subprocess.run(["certbot", "--version"], check=True, capture_output=True)
            except (subprocess.CalledProcessError, FileNotFoundError):
                logger.error("certbot not found. Please install certbot: apt-get install certbot")
                return False
            
            # Prepare certbot command
            cmd = [
                "certbot", "certonly",
                "--standalone",
                "--non-interactive",
                "--agree-tos",
                "--email", self.email,
                "--domains", self.domain,
                "--cert-path", str(self.letsencrypt_dir),
                "--key-path", str(self.letsencrypt_dir),
                "--fullchain-path", str(self.letsencrypt_dir / "fullchain.pem"),
            ]
            
            if self.staging:
                cmd.append("--staging")
                logger.info("Using Let's Encrypt staging environment")
            
            # Run certbot
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                # Copy certificates to our standard location
                letsencrypt_cert = self.letsencrypt_dir / f"{self.domain}/fullchain.pem"
                letsencrypt_key = self.letsencrypt_dir / f"{self.domain}/privkey.pem"
                
                if letsencrypt_cert.exists() and letsencrypt_key.exists():
                    # Create symlinks or copy files
                    import shutil
                    shutil.copy2(letsencrypt_cert, self.cert_file)
                    shutil.copy2(letsencrypt_key, self.key_file)
                    
                    logger.info(f"Let's Encrypt certificate installed: {self.cert_file}")
                    return True
                else:
                    logger.error("Let's Encrypt certificates not found after generation")
                    return False
            else:
                logger.error(f"certbot failed: {result.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to setup Let's Encrypt certificate: {e}")
            return False
    
    def renew_certificate(self) -> bool:
        """Renew Let's Encrypt certificate if needed"""
        try:
            if not self._has_valid_certificates():
                logger.info("No valid certificate found, generating new one...")
                return self.setup_letsencrypt_certificate()
            
            # Check if certificate expires within 30 days
            with open(self.cert_file, 'rb') as f:
                cert_data = f.read()
            cert = x509.load_pem_x509_certificate(cert_data)
            days_until_expiry = (cert.not_valid_after - datetime.utcnow()).days
            
            if days_until_expiry < 30:
                logger.info(f"Certificate expires in {days_until_expiry} days, renewing...")
                return self.setup_letsencrypt_certificate()
            else:
                logger.info(f"Certificate valid for {days_until_expiry} more days")
                return True
                
        except Exception as e:
            logger.error(f"Failed to renew certificate: {e}")
            return False
    
    def get_security_headers(self) -> Dict[str, str]:
        """Get security headers for HTTPS"""
        return {
            'Strict-Transport-Security': 'max-age=31536000; includeSubDomains; preload',
            'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY',
            'X-XSS-Protection': '1; mode=block',
            'Referrer-Policy': 'strict-origin-when-cross-origin',
            'Permissions-Policy': 'geolocation=(), microphone=(), camera=()',
            'Content-Security-Policy': (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "font-src 'self' data:; "
                "connect-src 'self' https:; "
                "frame-ancestors 'none';"
            )
        }
    
    def setup_certificates(self) -> bool:
        """Setup certificates based on environment"""
        if os.getenv("TLS_MODE", "auto") == "self-signed":
            return self.generate_self_signed_certificate()
        elif os.getenv("TLS_MODE", "auto") == "letsencrypt":
            return self.setup_letsencrypt_certificate()
        else:
            # Auto mode: try Let's Encrypt first, fallback to self-signed
            if self.domain != "localhost" and self.setup_letsencrypt_certificate():
                return True
            else:
                logger.info("Falling back to self-signed certificate")
                return self.generate_self_signed_certificate()

def create_ssl_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    """Create SSL context from certificate files"""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_file, key_file)
    context.set_ciphers('ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!MD5:!DSS')
    context.options |= ssl.OP_NO_SSLv2
    context.options |= ssl.OP_NO_SSLv3
    context.options |= ssl.OP_NO_TLSv1
    context.options |= ssl.OP_NO_TLSv1_1
    return context

def redirect_to_https(app):
    """Add HTTPS redirect middleware"""
    from flask import request, redirect, url_for
    
    @app.before_request
    def force_https():
        if not request.is_secure and app.config.get('FORCE_HTTPS', True):
            return redirect(request.url.replace('http://', 'https://', 1), code=301)

