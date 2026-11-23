from __future__ import annotations

import base64
import json

PREFIX = "J7r"
MARKER1 = "kD"
MARKER2 = "W4s"
SUFFIX = "nQ"

SOURCE_ALPHABET = "_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
TARGET_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
TRANS_TABLE = str.maketrans(SOURCE_ALPHABET, TARGET_ALPHABET)


def decode_images(payload: str) -> list[dict]:
    """Decode ManhuaFree / CloudMe obfuscated image payload into image dicts."""
    if (
        not isinstance(payload, str)
        or not payload.startswith(PREFIX)
        or not payload.endswith(SUFFIX)
    ):
        raise ValueError("Invalid payload format")

    body = payload[len(PREFIX) : -len(SUFFIX)]
    net_len = len(body) - len(MARKER1) - len(MARKER2)
    if net_len < 0:
        raise ValueError("Payload body too short")

    part3_len = net_len // 3
    part1_len = (net_len - part3_len) // 2
    part2_len = net_len - part3_len - part1_len

    part1 = body[:part1_len]
    m1 = body[part1_len : part1_len + len(MARKER1)]
    part2 = body[part1_len + len(MARKER1) : part1_len + len(MARKER1) + part2_len]
    m2 = body[
        part1_len + len(MARKER1) + part2_len : part1_len + len(MARKER1) + part2_len + len(MARKER2)
    ]
    part3 = body[part1_len + len(MARKER1) + part2_len + len(MARKER2) :]

    if m1 != MARKER1 or m2 != MARKER2 or len(part3) != part3_len:
        raise ValueError("Payload validation failed")

    recombined = part3 + part1 + part2

    chunk_size = 7
    chunks = []
    for i in range(0, len(recombined), chunk_size):
        chunk_idx = i // chunk_size
        chunk = recombined[i : i + chunk_size]
        if chunk_idx % 2 != 0:
            chunk = chunk[::-1]
        chunks.append(chunk)

    transformed = "".join(chunks)
    substituted = transformed.translate(TRANS_TABLE)

    padding_needed = (4 - len(substituted) % 4) % 4
    padded_b64 = substituted + ("=" * padding_needed)
    standard_b64 = padded_b64.replace("-", "+").replace("_", "/")

    raw_bytes = base64.b64decode(standard_b64)
    json_text = raw_bytes.decode("utf-8")
    return json.loads(json_text)
