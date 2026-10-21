#!/bin/sh
# Make the data volume writable for the app user, whatever the host did to its
# ownership, then run as that user. Nothing else runs as root.
set -e
mkdir -p /app/data
chown -R planenoise:planenoise /app/data 2>/dev/null || true
exec gosu planenoise "$@"
