#!/usr/bin/env python
"""Build the TS/NONCE/SIG header for an admin email.

Usage:
    python scripts/admin_sign.py "ADMIN: status"

Paste the printed lines into the email body below the exact subject you
signed — the signature is bound to that subject and expires after
ADMIN_REPLAY_WINDOW_SECONDS (default 300s), so sign right before sending.
"""
from __future__ import annotations

import sys
from getpass import getpass

from thenetwork.admin.auth import sign_admin_request


def main() -> None:
    if len(sys.argv) != 2:
        print('Usage: admin_sign.py "<exact ADMIN subject line>"', file=sys.stderr)
        raise SystemExit(1)
    subject = sys.argv[1]
    token = getpass("Admin token: ")
    print(sign_admin_request(token, subject))


if __name__ == "__main__":
    main()
