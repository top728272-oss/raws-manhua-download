"""Hipmh (嬉皮漫畫) chapter image payload decoder.

`GET /v2/chapter` answers with an ``images`` field that is one opaque string.
The site's own reader ships an obfuscated ``chapter-decoder.js`` that registers
``window.__cimg.r(payload)``; this module is a faithful port of what that script
does, so no JS runtime is needed.

Envelope layout, recovered from ``reader.hipmh.top/assets/runtime/chapter-decoder.js``::

    payload = "qM9" + seg1 + "Vx" + seg2 + "pL0" + seg3 + "Z7"

    net = len(seg1) + len(seg2) + len(seg3)
    len(seg3) = net // 3
    len(seg1) = (net - len(seg3)) // 2
    len(seg2) = the remainder

The three segments are rejoined as ``seg3 + seg1 + seg2``, de-interleaved in
7-char chunks (every odd-indexed chunk reversed), transcoded through a custom
base64 alphabet, and decoded to the JSON array of image paths.

Then :func:`drop_decoy` removes the planted decoy entry. The decoded list is one
entry LONGER than the real page count, and the extra one is a decoy that shares
a page number with a real page but carries a different content hash - so sorting
by page number cannot find it. Only the site's ``order_id``/``sid`` arithmetic
locates it.
"""

from __future__ import annotations

import base64
import json

PREFIX = "qM9"
SUFFIX = "Z7"
MARKER1 = "Vx"
MARKER2 = "pL0"


CHUNK_STEP = 7


SOURCE_ALPHABET = "_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
TARGET_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
TRANS_TABLE = str.maketrans(SOURCE_ALPHABET, TARGET_ALPHABET)


_SALT = ((40503 << 16) | 31153) & 0xFFFFFFFF
_PRIME = ((34283 << 16) | 51819) & 0xFFFFFFFF

_UINT32_MAX = 0xFFFFFFFF


def _deinterleave(text: str) -> str:
    """Reverse every odd-indexed 7-char chunk and concatenate."""
    chunks = []
    for index, start in enumerate(range(0, len(text), CHUNK_STEP)):
        chunk = text[start : start + CHUNK_STEP]
        chunks.append(chunk[::-1] if index % 2 else chunk)
    return "".join(chunks)


def _uint32(value: object) -> int | None:
    """Coerce to a 32-bit unsigned int the way the site's reader does, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > _UINT32_MAX:
        return None
    return number


def decode_images(payload: str) -> list[str]:
    """Decode a `/v2/chapter` ``images`` payload into ordered image paths.

    The result still contains the decoy entry; run it through :func:`drop_decoy`
    before treating the list as pages. Raises ``ValueError`` on a malformed
    payload.
    """
    if not isinstance(payload, str):
        raise ValueError("Payload is not a string")
    if not payload.startswith(PREFIX) or not payload.endswith(SUFFIX):
        raise ValueError("Invalid payload envelope")

    body = payload[len(PREFIX) : -len(SUFFIX)]
    net = len(body) - len(MARKER1) - len(MARKER2)
    if net <= 0:
        raise ValueError("Payload body too short to hold three segments")

    len_third = net // 3
    len_first = (net - len_third) // 2
    len_second = net - len_third - len_first

    cursor = 0
    seg_first = body[cursor : cursor + len_first]
    cursor += len_first
    marker1 = body[cursor : cursor + len(MARKER1)]
    cursor += len(MARKER1)
    seg_second = body[cursor : cursor + len_second]
    cursor += len_second
    marker2 = body[cursor : cursor + len(MARKER2)]
    cursor += len(MARKER2)
    seg_third = body[cursor:]

    if marker1 != MARKER1 or marker2 != MARKER2 or len(seg_third) != len_third:
        raise ValueError("Payload marker validation failed")

    transposed = _deinterleave(seg_third + seg_first + seg_second)
    encoded = transposed.translate(TRANS_TABLE)
    encoded += "=" * (-len(encoded) % 4)

    try:
        payload_json = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as err:
        raise ValueError(f"Payload did not decode to JSON: {err}") from err

    if not isinstance(payload_json, list):
        raise ValueError("Decoded payload is not a list")
    return [entry for entry in payload_json if isinstance(entry, str)]


def drop_decoy(images: list[str], order_id: object, sid: object) -> list[str]:
    """Remove the planted decoy page from a decoded image list.

    Mirrors the reader's own function exactly, including its fallback: when the
    computed index falls outside the list (or either input is not a 32-bit
    integer) the list is returned untouched, which is what the site does for the
    chapters that ship no decoy.

    The XOR below is arbitrary-precision. Truncating ``sid * _SALT`` to 32 bits
    first reproduces the site for only a small fraction of chapters and silently
    returns a wrong page list for the rest.
    """
    if not isinstance(images, list) or not images:
        return images

    count = len(images)
    order = _uint32(order_id)
    seed = _uint32(sid)
    if order is None or seed is None:
        return images

    offset = ((seed * _SALT) ^ (count * _PRIME)) % count
    target = order ^ offset
    if target >= count:
        return images
    return images[:target] + images[target + 1 :]
