"""Gunicorn settings for the Aircraft Noise server.

One worker, on purpose. The recorder keeps the noise buffer, the per-aircraft
tracks, the live snapshot and the alert state in memory and runs its own
polling threads; a second worker would poll the feeds twice and answer with
a different snapshot. Concurrency comes from threads instead.

    python -m gunicorn -c deploy/gunicorn.conf.py proxy_server:app
"""

import os

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8787")
workers = 1
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "8"))

timeout = int(os.getenv("GUNICORN_TIMEOUT", "90"))
graceful_timeout = 20
keepalive = 5

accesslog = os.getenv("GUNICORN_ACCESSLOG", "-")
errorlog = os.getenv("GUNICORN_ERRORLOG", "-")
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms'

# The app's threads start in the worker, so preloading would put them in the master.
preload_app = False

# TLS straight from Gunicorn when the app's certificates exist and TLS is not
# switched off. Behind a proxy or tunnel that terminates TLS, set TLS_DISABLED=true.
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cert = os.getenv("TLS_CERT_FILE", os.path.join(_root, "tls_certs", "server.crt"))
_key = os.getenv("TLS_KEY_FILE", os.path.join(_root, "tls_certs", "server.key"))
if os.getenv("TLS_DISABLED", "false").lower() != "true" and os.path.isfile(_cert) and os.path.isfile(_key):
    certfile = _cert
    keyfile = _key


def post_worker_init(worker):
    """Start the recorder as soon as the worker is up, not on the first visit."""
    import proxy_server
    proxy_server.start_background_services()
