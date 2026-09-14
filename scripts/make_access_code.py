"""
Generate an access code for a gated deployment, and its hash.

    python scripts/make_access_code.py

Prints two things:

  the CODE   give this to reviewers; it is what they type
  the HASH   put this in .env; the server compares against it

Storing the hash rather than the code means the working secret is absent from
the config file, the container image and the host's settings page. sha256 does
not run backwards, so reading the config gives an attacker nothing to type.

The code is drawn from secrets.choice, which uses the operating system's
cryptographic random source. Ambiguous characters (0/O, 1/l/I) are excluded so
the code survives being read aloud or copied by hand.
"""

from __future__ import annotations

import hashlib
import secrets

ALPHABET = "abcdefghijkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ23456789"
WORDS = 4
WORD_LEN = 5


def make_code() -> str:
    """A grouped code: long enough to resist guessing, short enough to type."""
    groups = [
        "".join(secrets.choice(ALPHABET) for _ in range(WORD_LEN))
        for _ in range(WORDS)
    ]
    return "-".join(groups)


def main() -> int:
    code = make_code()
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    bits = (len(ALPHABET) ** (WORDS * WORD_LEN)).bit_length()

    print("\n  ACCESS CODE  (give this to reviewers)\n")
    print(f"      {code}\n")
    print("  Add this line to .env  (the code itself does not go in the file)\n")
    print(f"      RXSIGNAL_ACCESS_CODE_HASH={digest}\n")
    print(f"  Strength: {WORDS * WORD_LEN} characters from a {len(ALPHABET)}-symbol "
          f"alphabet, about {bits} bits.")
    print("  Rate limiting caps guesses at 60/minute, so this is not brute-forceable.\n")
    print("  This code cannot be recovered from the hash. Save it now; to replace")
    print("  it, run this script again and update .env.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
