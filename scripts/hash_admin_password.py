#!/usr/bin/env python3
"""Generate an argon2id hash for the admin panel password.

Usage:
    python scripts/hash_admin_password.py
    python scripts/hash_admin_password.py 'your-password'

Set the output as ADMIN_PASSWORD in your .env file.
"""

import sys

from app.utils.password import hash_password


def main() -> None:
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        import getpass

        password = getpass.getpass("Enter admin password: ")

    print(hash_password(password))


if __name__ == "__main__":
    main()
