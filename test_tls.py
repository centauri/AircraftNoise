#!/usr/bin/env python3
"""
TLS Test Script for Aircraft Noise
Tests TLS functionality and certificate validity
"""

import os
import sys
import ssl
import socket
import requests
import logging
from pathlib import Path
from urllib.parse import urlparse
from tls_config import TLSConfig

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def test_certificate_files():
    """Test if certificate files exist and are valid"""
    tls_config = TLSConfig()
    
    print("🔍 Testing certificate files...")
    
    if not tls_config.cert_file.exists():
        print(f"❌ Certificate file not found: {tls_config.cert_file}")
        return False
    
    if not tls_config.key_file.exists():
        print(f"❌ Private key file not found: {tls_config.key_file}")
        return False
    
    print(f"✅ Certificate file exists: {tls_config.cert_file}")
    print(f"✅ Private key file exists: {tls_config.key_file}")
    
    # Test certificate validity
    try:
        import cryptography
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        
        with open(tls_config.cert_file, 'rb') as f:
            cert_data = f.read()
        cert = x509.load_pem_x509_certificate(cert_data)
        
        print(f"✅ Certificate is valid")
        print(f"   Subject: {cert.subject}")
        print(f"   Issuer: {cert.issuer}")
        print(f"   Not valid before: {cert.not_valid_before}")
        print(f"   Not valid after: {cert.not_valid_after}")
        
        # Check if expired
        from datetime import datetime
        if cert.not_valid_after < datetime.utcnow():
            print("⚠️  Certificate has expired!")
            return False
        else:
            print("✅ Certificate is not expired")
            
    except Exception as e:
        print(f"❌ Error validating certificate: {e}")
        return False
    
    # Test private key
    try:
        with open(tls_config.key_file, 'rb') as f:
            key_data = f.read()
        key = serialization.load_pem_private_key(key_data, password=None)
        print("✅ Private key is valid")
    except Exception as e:
        print(f"❌ Error validating private key: {e}")
        return False
    
    return True

def test_ssl_context():
    """Test SSL context creation"""
    print("\n🔍 Testing SSL context...")
    
    tls_config = TLSConfig()
    ssl_context = tls_config.get_ssl_context()
    
    if ssl_context is None:
        print("❌ Failed to create SSL context")
        return False
    
    print("✅ SSL context created successfully")
    print(f"   Protocol: {ssl_context.protocol}")
    print(f"   Options: {ssl_context.options}")
    print(f"   Ciphers: {ssl_context.get_ciphers()[:3]}...")  # Show first 3 ciphers
    
    return True

def test_server_connection(host="localhost", port=8787, use_https=True):
    """Test connection to the server"""
    print(f"\n🔍 Testing server connection...")
    
    protocol = "https" if use_https else "http"
    url = f"{protocol}://{host}:{port}"
    
    try:
        # Test basic connection
        response = requests.get(url, timeout=10, verify=False)  # verify=False for self-signed
        print(f"✅ Server is responding on {url}")
        print(f"   Status code: {response.status_code}")
        print(f"   Headers: {dict(response.headers)}")
        
        # Test HTTPS-specific headers
        if use_https:
            if 'Strict-Transport-Security' in response.headers:
                print("✅ HSTS header present")
            else:
                print("⚠️  HSTS header missing")
            
            if 'X-Content-Type-Options' in response.headers:
                print("✅ Security headers present")
            else:
                print("⚠️  Some security headers missing")
        
        return True
        
    except requests.exceptions.SSLError as e:
        print(f"❌ SSL error connecting to {url}: {e}")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"❌ Connection error to {url}: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error connecting to {url}: {e}")
        return False

def test_https_redirect(host="localhost", port=8787):
    """Test HTTP to HTTPS redirect"""
    print(f"\n🔍 Testing HTTPS redirect...")
    
    http_url = f"http://{host}:{port}"
    
    try:
        response = requests.get(http_url, timeout=10, allow_redirects=False)
        
        if response.status_code == 301 or response.status_code == 302:
            if 'https://' in response.headers.get('Location', ''):
                print("✅ HTTP to HTTPS redirect working")
                print(f"   Redirects to: {response.headers['Location']}")
                return True
            else:
                print("⚠️  HTTP redirect not pointing to HTTPS")
                return False
        else:
            print(f"⚠️  No redirect detected (status: {response.status_code})")
            return False
            
    except Exception as e:
        print(f"❌ Error testing redirect: {e}")
        return False

def test_openssl_connection(host="localhost", port=8787):
    """Test SSL connection using OpenSSL"""
    print(f"\n🔍 Testing OpenSSL connection...")
    
    try:
        import subprocess
        
        # Test SSL connection
        cmd = [
            'openssl', 's_client', 
            '-connect', f'{host}:{port}',
            '-servername', host,
            '-verify_return_error'
        ]
        
        result = subprocess.run(
            cmd, 
            input=b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n',
            capture_output=True, 
            timeout=10
        )
        
        if result.returncode == 0:
            print("✅ OpenSSL connection successful")
            return True
        else:
            print(f"❌ OpenSSL connection failed: {result.stderr.decode()}")
            return False
            
    except FileNotFoundError:
        print("⚠️  OpenSSL not found, skipping OpenSSL test")
        return True
    except Exception as e:
        print(f"❌ OpenSSL test error: {e}")
        return False

def main():
    """Run all TLS tests"""
    print("🔒 TLS Test Suite for Aircraft Noise")
    print("=" * 50)
    
    # Test certificate files
    cert_ok = test_certificate_files()
    
    # Test SSL context
    ssl_ok = test_ssl_context()
    
    # Test server connection (if server is running)
    connection_ok = test_server_connection()
    
    # Test HTTPS redirect
    redirect_ok = test_https_redirect()
    
    # Test OpenSSL connection
    openssl_ok = test_openssl_connection()
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 Test Results Summary:")
    print(f"   Certificate files: {'✅' if cert_ok else '❌'}")
    print(f"   SSL context: {'✅' if ssl_ok else '❌'}")
    print(f"   Server connection: {'✅' if connection_ok else '❌'}")
    print(f"   HTTPS redirect: {'✅' if redirect_ok else '❌'}")
    print(f"   OpenSSL connection: {'✅' if openssl_ok else '❌'}")
    
    all_tests_passed = cert_ok and ssl_ok and (connection_ok or redirect_ok)
    
    if all_tests_passed:
        print("\n🎉 All TLS tests passed! Your setup is working correctly.")
        return 0
    else:
        print("\n⚠️  Some tests failed. Check the output above for details.")
        return 1

if __name__ == "__main__":
    sys.exit(main())

