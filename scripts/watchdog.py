#!/usr/bin/env python3
"""Outside watchdog for the Aircraft Noise recorder.

Runs from a systemd timer (or cron) every few minutes, independent of the
server process. It asks the running server for /api/health. When the server
cannot be reached, or reports that the recorder stopped, it sends one alert
through the same email and ntfy settings the admin page holds, sends one
message when the server is back, and can restart the systemd unit after a
number of failed checks in a row.

    python3 scripts/watchdog.py --restart-unit planenoise.service

Settings are read straight from the SQLite database, so an alert still goes out
when the web server is down.
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from database import PlaneNoiseDB   # noqa: E402
import notifications                # noqa: E402


def fetch_health(url, timeout):
    """(reachable, payload). The server's own certificate is self-signed on the LAN."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"User-Agent": "plane-noise-watchdog/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return True, json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, {"error": str(e)[:200]}


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"failures": 0, "alerted": False, "since": None}


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def restart_unit(unit):
    command = ["systemctl", "restart", unit]
    if os.geteuid() != 0:
        command = ["sudo", "-n"] + command     # the sudoers line in scripts/README.md allows this one command
    try:
        subprocess.run(command, check=True, timeout=60)
        return f"restarted {unit}"
    except (subprocess.SubprocessError, OSError) as e:
        return f"restart of {unit} failed: {e}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="https://127.0.0.1:8787/api/health")
    parser.add_argument("--db", default=os.path.join(ROOT, "plane_noise.db"))
    parser.add_argument("--state", default=os.path.join(ROOT, "data", "watchdog_state.json"))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--restart-unit", default="", help="systemd unit to restart after repeated failures")
    parser.add_argument("--restart-after", type=int, default=3, help="failed checks in a row before a restart")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    db = PlaneNoiseDB(args.db)
    settings = notifications.read_settings(db.get_admin_setting)
    state = load_state(args.state)
    reachable, health = fetch_health(args.url, args.timeout)
    healthy = reachable and health.get("ok", False)
    now = time.time()

    if healthy:
        if state.get("alerted"):
            down_for = now - float(state.get("since") or now)
            notifications.deliver(settings, "Aircraft Noise server back",
                                  f"The server answers again after {int(down_for // 60)} min.", priority="default",
                                  tags=["white_check_mark"])
        state = {"failures": 0, "alerted": False, "since": None}
        save_state(args.state, state)
        if not args.quiet:
            print("ok")
        return 0

    state["failures"] = int(state.get("failures", 0)) + 1
    state["since"] = state.get("since") or now
    reason = (f"No answer from {args.url}: {health.get('error')}" if not reachable
              else f"Server reports a problem: {', '.join(health.get('failing') or ['unknown'])}")
    action = ""
    if args.restart_unit and state["failures"] >= args.restart_after:
        action = restart_unit(args.restart_unit)
        state["failures"] = 0
    if not state.get("alerted"):
        delivered, _detail = notifications.deliver(
            settings, "Aircraft Noise server down",
            f"{reason}\nFailed checks in a row: {state['failures']}\n{action}".strip(), priority="urgent", tags=["rotating_light"])
        state["alerted"] = bool(delivered)
    save_state(args.state, state)
    if not args.quiet:
        print(f"down: {reason} {action}".strip())
    return 1


if __name__ == "__main__":
    sys.exit(main())
