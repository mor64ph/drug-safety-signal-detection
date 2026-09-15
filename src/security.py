"""
Comparison primitives for secrets.

One function, because getting it slightly wrong is a live defect (D-03) rather
than a style problem.
"""
from __future__ import annotations

import hmac


def constant_time_equal(supplied: object, expected: object) -> bool:
    """Whether two secrets match, in time independent of how far they agree.

    `hmac.compare_digest` accepts two str only if both are ASCII, and raises
    TypeError otherwise. Every value compared here arrives from a request -- a
    query parameter, a form field, a cookie -- so a caller that passes the raw
    string is one non-ASCII character away from an unhandled exception.

    That is not theoretical. `/admin/analytics?admin_code=wrong` returned 404
    as designed, while `?admin_code=wrÖng` returned **500**. The 404 is
    supposed to make the endpoint indistinguishable from one that does not
    exist; a 500 told an attacker an admin code was configured, defeating the
    whole arrangement in one request. The CSRF check had the same shape, so a
    garbled token produced a 500 instead of a 400.

    Encoding to UTF-8 first makes every comparison byte-wise and total. A
    missing or empty value is False rather than an error, so callers can pass
    whatever the request gave them.
    """
    if not supplied or not expected:
        return False
    if not isinstance(supplied, (str, bytes)) or not isinstance(expected, (str, bytes)):
        return False
    left = supplied.encode("utf-8") if isinstance(supplied, str) else supplied
    right = expected.encode("utf-8") if isinstance(expected, str) else expected
    return hmac.compare_digest(left, right)
