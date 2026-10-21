# Keeping the recorder alive

Three layers, each covering what the previous one cannot.

1. **systemd restarts the process** when it crashes: `planenoise.service`, `Restart=always`.
   The unit runs Gunicorn with one threaded worker (`deploy/gunicorn.conf.py`). One worker
   on purpose: the recorder keeps its noise buffer, tracks and live snapshot in memory.
   Gunicorn serves TLS itself when `tls_certs/server.crt` and `.key` exist and
   `TLS_DISABLED` is not set, so a tunnel pointed at https://127.0.0.1:8787 keeps working.
2. **The server watches its own feeds** every minute and alerts by ntfy or email when the
   noise sensor goes quiet, the aircraft feed keeps failing, the polling loop stalls, the
   database errors, or the OpenSky allowance runs low. Configure it under Admin → Settings →
   Notifications and press "Send test". One message when a problem starts, one when it clears,
   a reminder after a day.
3. **The outside watchdog** (`watchdog.py`, run by `planenoise-watchdog.timer` every five minutes)
   asks the server for `/api/health`. If the server does not answer, or reports the recorder
   stopped, it alerts through the same settings, read straight from the database, and after
   three failed checks in a row restarts the service.

Install on the Debian host, from the project directory:

```
sudo bash scripts/install_service.sh
```

The watchdog restarts the unit with `systemctl`, so the service user needs that one command
without a password:

```
sudo visudo
<user> ALL=(root) NOPASSWD: /bin/systemctl restart planenoise.service
```

Email uses `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USERNAME`, `MAIL_PASSWORD` and
`MAIL_DEFAULT_SENDER` from `.env`, which the units load. ntfy needs only a topic URL.

Check by hand at any time:

```
.venv/bin/python3 scripts/watchdog.py
systemctl status planenoise.service
journalctl -u planenoise.service -n 50
```
