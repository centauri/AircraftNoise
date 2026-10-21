#!/usr/bin/env python3
"""Back up the Aircraft Noise database by hand or from cron.

    python3 scripts/backup_db.py            # into data/backups, keeps the configured number
    python3 scripts/backup_db.py --keep 14

The server also does this once a night after 03:00 local time.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--keep", type=int, default=0, help="copies to keep (default: the admin setting, 7)")
    args = parser.parse_args()
    import proxy_server
    path = proxy_server.backup_database(keep=args.keep or None)
    print(path, f"{os.path.getsize(path) / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
