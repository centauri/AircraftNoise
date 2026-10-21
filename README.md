# Aircraft Noise

Real aircraft noise, measured on a local sensor and matched to the flights overhead.
A live station: [uitgeest.aircraftnoise.eu](https://uitgeest.aircraftnoise.eu). Project page: [aircraftnoise.eu](https://aircraftnoise.eu).

The recorder polls aircraft positions (OpenSky, FlightAware or Airplanes.live) and the
noise sensor (Sensor.Community), stores every pass inside the detection circle, and
assigns each noise peak to the one aircraft closest to the microphone when it was
recorded. The public page shows the passes, the map with flight paths and live traffic,
and the aircraft photos; the admin pages hold the settings, feeds, zone and alerts.

## Run it

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp env_example.txt .env            # mail settings, sensor position, TLS
deploy/run_gunicorn.sh             # https://localhost:8787, admin at /admin
```

`deploy/run_gunicorn.sh` runs the server under Gunicorn with one threaded worker, which
is how the systemd service and the container run it. `python3 proxy_server.py` starts
the development server instead; fine on a laptop, not for a site.

The first visit to `/admin` opens the walkthrough: create the administrator account,
place the microphone on the map, pick the noise sensor and the aircraft feed, and
optionally set up alerts. Everything in it can be changed later under Settings.

Or with Docker. The compose file pulls the published image, for amd64 and
arm64 (a Raspberry Pi 4 or 5 with a 64-bit OS, or any x86 box), and keeps the
database in `./data`. No `.env` is needed; paste it into Portainer or the
Unraid Compose Manager as it is, or:

```
docker compose up -d
```

Then open `http://<host>:8787/setup` for the walkthrough. Without compose:

```
docker run -d --name planenoise -p 8787:8787 -v ./data:/app/data \
  -e TLS_DISABLED=true -e FORCE_HTTPS=false ghcr.io/centauri/aircraftnoise:latest
```

A plain LAN address and a site behind TLS both work as they are: the admin
cookie is marked Secure only on HTTPS visits. Developers build from the
checkout by swapping the `image:` line for `build: .`. Each release also
carries a source zip for an install without git.

Inside the container the server speaks plain HTTP on 8787; terminate TLS on the
proxy or tunnel in front of it. On a plain Debian host, `scripts/install_service.sh`
installs the systemd service with auto-restart and the outside watchdog.

Forgot the admin password? With a mail server set under Admin → Alerts (or
`MAIL_*` in the environment) the login page mails a link. Without one, on the box:
`python3 scripts/reset_password.py you@example.com`, or in the container
`docker exec planenoise python scripts/reset_password.py you@example.com`.

## Layout

| Path | What it is |
|---|---|
| `proxy_server.py` | Flask app: recorder, matching, API, admin |
| `database.py` | SQLite schema and queries; `PN_DB_PATH` moves the file; `TLS_DISABLED=true` for plain HTTP |
| `feeds.py` | Catalog of aircraft and noise feeds |
| `notifications.py` | Alerts by ntfy or email |
| `community.py` | Sharing with the data service at data.aircraftnoise.eu, and the community page |
| `guard.py` | Scraper guard: budgets per address, trap link, escalating bans |
| `analytics.py` | Visitor statistics without personal data, bounded tables |
| `geo.py` | Country and city for an address: DB-IP table in `data/geo.db`, optional GeoLite2 city file |
| `tls_config.py` | Self-signed or Let's Encrypt certificates for the built-in server |
| `index.html`, `callsign.html`, `types.html` | Public pages (Dutch comes from the translation table) |
| `routes.py`, `aircraft_types.py` | Runway labels from OurAirports; noise ranking per aircraft family |
| `admin_*.html`, `admin_shell.html` | Admin pages |
| `static/` | Scripts, styles, fonts, flags, brand assets |
| `scripts/` | systemd units, outside watchdog, installer |
| `deploy/` | Gunicorn settings and launcher, nginx example |
| `tests/` | `python3 -m unittest discover -s tests` |

Runtime files stay out of git: `plane_noise.db`, `data/`, `tls_certs/`, `.env`.

## Settings that matter

- **Detection**: radius over the ground, altitude cap, and the noise slant range. A
  decibel is assigned only when the straight line to the microphone is within that range.
- **Feeds**: which position feed is active and how often it polls. OpenSky floors at 30 s.
- **Airports and routes**: press "Find airports near the sensor" once. Runways come from
  OurAirports, and every pass is then labelled as an arrival on a runway, a departure from
  one, or an overflight. Set where noise reports go in the same card; nothing is tied to one
  airport.
- **Aircraft registry**: make, model, registration and operator for about 620,000 aircraft,
  from the Mictronics aircraft database via tar1090-db, downloaded into the site's own
  database and refreshed weekly, plus every airline ICAO designator worldwide from Wikidata
  (CC0). Every pass is identified locally, no API calls.
- **Position privacy**: the public page and API show a fixed decoy point up to 200 m from
  the sensor, and the map marker wanders around it on every zoom, so the address is never
  marked. The radius is a setting; the recorder keeps using the real position.
- **Notifications**: ntfy or email alerts when the sensor, the feed or the recorder stops.
- **Visitor statistics**: counted on the server, no script, no cookie, no address stored. A
  visitor is a hash that changes every day and lives in memory for that day. The tables hold
  one row per day for ever and hour rows and per-kind rows for 400 days, at most 100 keys per
  kind per day, so they cannot grow out of hand. Admin → Analytics shows visitors, pages,
  referrers, campaign tags (`?utm_source=…&utm_campaign=…`), languages, countries, cities,
  devices, JSON use, bots, and whether people look when it is loud. Countries come from the
  Cloudflare header when there is one, else from DB-IP's free country table, downloaded
  monthly into `data/geo.db` (IP geolocation by DB-IP, CC BY 4.0). Cities appear when
  MaxMind's free GeoLite2-City.mmdb is placed in `data/`. The old tracker's tables are no
  longer written; a button there deletes them.
- **Scraper guard**: every address gets a budget of pages and JSON calls; scrapers that keep
  going, follow the hidden trap link or read pages with a script are blocked for a while,
  people are not. Admin → Settings → Server shows the blocked addresses. What it does and
  the Cloudflare setup to put in front of it: `docs/SCRAPERS.md`.

See `INSTALL.md` for the longer setup notes and `TLS_SETUP.md` for certificates.

## Licence

GNU Affero General Public License v3.0, see `LICENSE`. Run it, change it,
share it; if you offer a changed version as a service, publish your changes
too. The data service and the project page live in their own repositories.
