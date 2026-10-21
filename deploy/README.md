Production deployment quickstart

1) Gunicorn
- Install: pip install gunicorn
- Run locally: ./deploy/run_gunicorn.sh
- Env knobs (optional): GUNICORN_* (bind, workers, threads, loglevel)

2) Nginx + TLS
- Copy deploy/nginx.conf to /etc/nginx/sites-available/plane-noise and update:
  - server_name example.com -> your domain
  - static alias path if different
  - cert paths (Let’s Encrypt recommended)
- Enable site and reload Nginx

3) Let’s Encrypt
- Install certbot (e.g., apt install certbot python3-certbot-nginx)
- certbot --nginx -d example.com

4) Systemd (example)
- Create /etc/systemd/system/plane-noise.service:
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

- systemctl daemon-reload && systemctl enable --now plane-noise

Notes
- Background polling is started via Flask before_first_request, safe under Gunicorn.
- Keep SQLite in WAL mode for durability; back up plane_noise.db periodically.
- Put admin behind HTTPS; consider IP allowlist and strong passwords.
