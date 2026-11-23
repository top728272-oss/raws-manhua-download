"""Signed image URL expiry detection.

Some CDNs sign page image URLs with an absolute expiry embedded in the query
string (shimolife ``x-expires``, hamreus/wszwhg ``e``). A URL whose signature
has already lapsed is rejected deterministically by the CDN's anti-leech layer
(403 "ACCESS DENIED") no matter how often it is retried, so the downloader asks
this module whether a resolved URL is dead on arrival and skips the doomed
fetch instead of burning the retry budget on it.

Only absolute epoch-style values are recognised. Relative TTLs, hex blobs and
lookalike parameters are ignored (the URL is treated as unexpiring) so a wrong
guess can never cause a valid URL to be skipped.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_EPOCH_MIN = 1_500_000_000
_EPOCH_MAX = 4_102_444_800

_DIGITS = re.compile(r"^\d{1,13}$")


_PARAMS = ("x-expires", "x-oss-expires", "expires", "e")


def url_expiry(url: str) -> float | None:
    """Expiry epoch of a signed URL, or None when it carries no recognisable one."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return None
    if not query:
        return None
    params = parse_qs(query, keep_blank_values=True)
    for name in _PARAMS:
        values = params.get(name)
        if not values:
            continue
        raw = values[0].strip()
        if not raw.isascii() or not _DIGITS.match(raw):
            continue
        value = int(raw)
        if value > 1_000_000_000_000:
            value //= 1000
        if _EPOCH_MIN <= value <= _EPOCH_MAX:
            return float(value)
    return None
