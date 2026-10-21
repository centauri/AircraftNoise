# TLS/HTTPS Setup Guide

This guide explains how to set up TLS/HTTPS for the Aircraft Noise application, supporting both self-signed certificates for development and Let's Encrypt for production.

## Overview

The application now includes comprehensive TLS support with:
- **Self-signed certificates** for development and testing
- **Let's Encrypt integration** for production with automatic renewal
- **Automatic HTTPS redirect** when TLS is enabled
- **Security headers** for enhanced protection
- **Graceful fallback** to HTTP if TLS setup fails

## Quick Start

### 1. Development Setup (Self-Signed)

For local development, the simplest approach is to use self-signed certificates:

```bash
# Set environment variables
export TLS_DOMAIN="localhost"
export TLS_EMAIL="admin@localhost"
export TLS_MODE="self-signed"
export FORCE_HTTPS="true"

# Run the setup script
./setup_tls.sh

# Start the server
python3 proxy_server.py
```

The server will start on `https://localhost:8787` with a self-signed certificate.

### 2. Production Setup (Let's Encrypt)

For production deployment with a real domain:

```bash
# Set your domain and email
export TLS_DOMAIN="your-domain.com"
export TLS_EMAIL="your-email@example.com"
export TLS_MODE="letsencrypt"
export FORCE_HTTPS="true"

# Run the setup script
./setup_tls.sh

# Start the server
python3 proxy_server.py
```

### 3. Cross-Platform Production Deployment

The setup script automatically detects your operating system and installs the appropriate packages:

```bash
# Works on Ubuntu, Debian, CentOS, RHEL, Fedora, Alpine, Arch, openSUSE, macOS
export TLS_DOMAIN="your-domain.com"
export TLS_EMAIL="your-email@example.com"
chmod +x setup_tls.sh
./setup_tls.sh
```

**Supported Platforms:**
- **Linux**: Ubuntu, Debian, CentOS, RHEL, Fedora, Rocky Linux, AlmaLinux, Alpine, Arch, openSUSE
- **macOS**: With Homebrew
- **Docker**: Multi-architecture support

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TLS_DOMAIN` | `localhost` | Domain name for the certificate |
| `TLS_EMAIL` | `admin@localhost` | Email for Let's Encrypt registration |
| `TLS_MODE` | `auto` | Certificate mode: `self-signed`, `letsencrypt`, or `auto` |
| `FORCE_HTTPS` | `true` | Force HTTPS redirects |
| `LETSENCRYPT_STAGING` | `false` | Use Let's Encrypt staging environment |
| `PYTHON_CMD` | `python3` | Python command to use |
| `PIP_CMD` | `pip3` | Pip command to use |

## Certificate Modes

### Self-Signed Mode (`TLS_MODE=self-signed`)

- **Use case**: Development, testing, internal networks
- **Pros**: No external dependencies, works immediately
- **Cons**: Browser warnings, not trusted by default
- **Validity**: 365 days

### Let's Encrypt Mode (`TLS_MODE=letsencrypt`)

- **Use case**: Production with public domain
- **Pros**: Trusted by all browsers, free, automatic renewal
- **Cons**: Requires public domain, port 80 access
- **Validity**: 90 days (auto-renewed)

### Auto Mode (`TLS_MODE=auto`)

- **Behavior**: 
  - Uses Let's Encrypt if domain is not `localhost`
  - Falls back to self-signed for `localhost`
- **Use case**: Flexible deployment

## Manual Setup

If you prefer to set up certificates manually:

### Self-Signed Certificate

```bash
# Create certificate directory
mkdir -p tls_certs

# Generate private key
openssl genrsa -out tls_certs/server.key 2048

# Generate certificate
openssl req -new -x509 -key tls_certs/server.key -out tls_certs/server.crt -days 365 \
    -subj "/C=US/ST=Development/L=Local/O=Aircraft Noise/CN=your-domain.com" \
    -addext "subjectAltName=DNS:your-domain.com,DNS:localhost,DNS:127.0.0.1,IP:127.0.0.1"
```

### Let's Encrypt Certificate

```bash
# Install certbot
sudo apt-get install certbot  # Ubuntu/Debian
# or
sudo yum install certbot      # CentOS/RHEL
# or
brew install certbot          # macOS

# Generate certificate
sudo certbot certonly --standalone \
    --non-interactive \
    --agree-tos \
    --email your-email@example.com \
    --domains your-domain.com

# Copy certificates
sudo cp /etc/letsencrypt/live/your-domain.com/fullchain.pem tls_certs/server.crt
sudo cp /etc/letsencrypt/live/your-domain.com/privkey.pem tls_certs/server.key
sudo chown $(whoami):$(whoami) tls_certs/server.crt tls_certs/server.key
sudo chmod 600 tls_certs/server.key
sudo chmod 644 tls_certs/server.crt
```

## Security Features

### Security Headers

The application automatically adds security headers when HTTPS is enabled:

- **Strict-Transport-Security**: Forces HTTPS connections
- **X-Content-Type-Options**: Prevents MIME type sniffing
- **X-Frame-Options**: Prevents clickjacking
- **X-XSS-Protection**: Enables XSS filtering
- **Referrer-Policy**: Controls referrer information
- **Permissions-Policy**: Restricts browser features
- **Content-Security-Policy**: Controls resource loading

### HTTPS Redirect

When `FORCE_HTTPS=true`, all HTTP requests are automatically redirected to HTTPS:

```python
# Automatic redirect: http://example.com → https://example.com
```

### Session Security

Session cookies are automatically secured when HTTPS is enabled:

- `SESSION_COOKIE_SECURE`: Only sent over HTTPS
- `SESSION_COOKIE_HTTPONLY`: Not accessible via JavaScript
- `SESSION_COOKIE_SAMESITE`: CSRF protection

## Troubleshooting

### Common Issues

#### 1. Certificate Not Found

**Error**: `No valid certificates found, running without TLS`

**Solution**: Run the setup script or check certificate files exist:
```bash
ls -la tls_certs/
```

#### 2. Let's Encrypt Validation Failed

**Error**: `Let's Encrypt certificate generation failed`

**Solutions**:
- Ensure port 80 is accessible
- Check domain DNS resolution
- Verify email address is valid
- Try staging environment first: `LETSENCRYPT_STAGING=true`

#### 3. Browser Security Warning

**Issue**: Browser shows "Not Secure" or certificate warning

**Solutions**:
- For self-signed: Click "Advanced" → "Proceed to site"
- For Let's Encrypt: Check domain configuration
- For development: Add certificate to trusted store

#### 4. Certificate Expired

**Error**: `Certificate expired`

**Solution**: Renew certificate:
```bash
# For Let's Encrypt
sudo certbot renew

# For self-signed
./setup_tls.sh
```

### Debugging

Enable debug logging to troubleshoot TLS issues:

```bash
export FLASK_DEBUG=1
python3 proxy_server.py
```

Check certificate validity:

```bash
# Check certificate
openssl x509 -in tls_certs/server.crt -text -noout

# Check private key
openssl rsa -in tls_certs/server.key -check -noout

# Test SSL connection
openssl s_client -connect localhost:8787 -servername localhost
```

## Production Deployment

### Nginx Reverse Proxy

For production, it's recommended to use Nginx as a reverse proxy:

```nginx
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;
    
    ssl_certificate /path/to/tls_certs/server.crt;
    ssl_certificate_key /path/to/tls_certs/server.key;
    
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!MD5:!DSS;
    ssl_prefer_server_ciphers off;
    
    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Docker Deployment

```dockerfile
FROM python:3.9-slim

# Install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy application
COPY . .

# Setup TLS
RUN chmod +x setup_tls.sh
RUN ./setup_tls.sh

# Expose ports
EXPOSE 8787

# Start application
CMD ["python3", "proxy_server.py"]
```

### Systemd Service

Create `/etc/systemd/system/plane-noise.service`:

```ini
[Unit]
Description=Aircraft Noise
After=network.target

[Service]
Type=simple
User=plane-noise
WorkingDirectory=/opt/plane-noise
Environment=TLS_DOMAIN=your-domain.com
Environment=TLS_EMAIL=your-email@example.com
Environment=FORCE_HTTPS=true
ExecStart=/usr/bin/python3 proxy_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Best Practices

1. **Always use HTTPS in production**
2. **Set up automatic certificate renewal**
3. **Use strong cipher suites**
4. **Implement proper security headers**
5. **Monitor certificate expiration**
6. **Use HSTS for additional security**
7. **Regular security updates**

## Support

For issues related to TLS setup:

1. Check the troubleshooting section above
2. Review application logs
3. Verify certificate validity
4. Test with different browsers
5. Check firewall and network configuration

## Security Considerations

- Self-signed certificates are only suitable for development
- Always use Let's Encrypt or commercial certificates for production
- Keep certificates and private keys secure
- Monitor for certificate expiration
- Use strong private keys (2048+ bits)
- Implement proper backup procedures
