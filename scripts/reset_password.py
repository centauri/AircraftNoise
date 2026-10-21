#!/usr/bin/env python3
"""Set a new password for an admin account, from the shell of the box the
station runs on. For when the mail-based reset is not available.

    python3 scripts/reset_password.py you@example.com            # prints a new random password
    python3 scripts/reset_password.py you@example.com --password 'a new one of 10+ characters'

In the container:  docker exec planenoise python scripts/reset_password.py you@example.com
"""
import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Set a new password for an admin account.")
    parser.add_argument("email")
    parser.add_argument("--password", help="the new password (10 characters or more); made up when absent")
    args = parser.parse_args(argv)
    from database import db
    from proxy_server import hash_password
    user = db.get_user_by_email_with_hash(args.email.strip().lower())
    if not user:
        print(f"No account with the address {args.email}.", file=sys.stderr)
        return 1
    password = args.password or secrets.token_urlsafe(12)
    if len(password) < 10:
        print("The password needs 10 characters or more.", file=sys.stderr)
        return 1
    db.update_user(user["id"], password_hash=hash_password(password), is_active=True)
    if args.password:
        print(f"Password changed for {user['email']}.")
    else:
        print(f"Password changed for {user['email']}. The new password is:\n\n    {password}\n\nSign in with it and set your own under the admin's user page.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
