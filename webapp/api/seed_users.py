"""Creates or resets a DES account. No self-serve signup by design - see
security.py. Run once per person when onboarding the 15 DES:

    python -m webapp.api.seed_users add des1@akowe.example "Jane Doe" --admin
    (prompts for a password; --admin only for whoever should be able to
    manage accounts, not needed for ordinary review work)
"""

import argparse
import getpass
import sys
from pathlib import Path

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from . import db, security  # noqa: E402


def add_user(email: str, full_name: str, is_admin: bool):
    db.init_db()
    email = email.lower().strip()
    password = getpass.getpass(f"Password for {email}: ")
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)
    password_hash = security.hash_password(password)
    with db.engine.begin() as conn:
        existing = conn.execute(select(db.users.c.id).where(db.users.c.email == email)).first()
        if existing:
            conn.execute(update(db.users).where(db.users.c.id == existing[0]).values(
                password_hash=password_hash, full_name=full_name, is_admin=is_admin))
            print(f"Updated {email}")
        else:
            conn.execute(db.users.insert().values(
                email=email, password_hash=password_hash, full_name=full_name, is_admin=is_admin))
            print(f"Created {email}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="Create or reset one DES account")
    a.add_argument("email")
    a.add_argument("full_name")
    a.add_argument("--admin", action="store_true")
    args = ap.parse_args()
    if args.cmd == "add":
        add_user(args.email, args.full_name, args.admin)


if __name__ == "__main__":
    main()
