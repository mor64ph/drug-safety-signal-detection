"""
Generate the key that signs session cookies.

    python scripts/make_secret_key.py

Prints one line to paste into .env.

Flask signs the session cookie with this value. Without a fixed one the server
invents a new key at startup, which means every restart signs every user out and
invalidates the half-finished password resets of anyone who was mid-flow. With a
guessable one, anyone who can guess it can forge a session cookie for any
account, so it comes from the operating system's cryptographic random source and
nowhere else.

Treat it like a password: it belongs in .env or in the host's secret store,
never in the repository. Replacing it is safe -- it only signs everyone out.
"""

from __future__ import annotations

import secrets


def main() -> int:
    key = secrets.token_hex(32)
    print("\n  Add this line to .env  (or set it in your host's environment)\n")
    print(f"      RXSIGNAL_SECRET_KEY={key}\n")
    print("  256 bits from os.urandom. Do not commit it, do not reuse it across")
    print("  deployments, and do not paste it into a chat window. Changing it")
    print("  signs everyone out and breaks pending reset links; nothing else.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
