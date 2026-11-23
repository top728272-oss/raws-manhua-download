"""Minimal pure-Python AES-128 decryption (CBC + PKCS#7).

Several manga sites hand the browser an AES-encrypted payload that a bundled
copy of CryptoJS decrypts client-side (pufeimh's reader is one). We need the
same plaintext, but the project deliberately keeps its dependency list small -
there is no ``cryptography`` or ``pycryptodome`` - so this module implements
just enough AES to decrypt.

Only *decryption* is provided; that is all any plugin needs. Correctness is
pinned by the FIPS-197 AES-128 test vector plus a live round-trip test against
pufeimh's own page data.

Reference: FIPS-197 (Advanced Encryption Standard).
"""

from __future__ import annotations

_SBOX = (
    0x63,
    0x7C,
    0x77,
    0x7B,
    0xF2,
    0x6B,
    0x6F,
    0xC5,
    0x30,
    0x01,
    0x67,
    0x2B,
    0xFE,
    0xD7,
    0xAB,
    0x76,
    0xCA,
    0x82,
    0xC9,
    0x7D,
    0xFA,
    0x59,
    0x47,
    0xF0,
    0xAD,
    0xD4,
    0xA2,
    0xAF,
    0x9C,
    0xA4,
    0x72,
    0xC0,
    0xB7,
    0xFD,
    0x93,
    0x26,
    0x36,
    0x3F,
    0xF7,
    0xCC,
    0x34,
    0xA5,
    0xE5,
    0xF1,
    0x71,
    0xD8,
    0x31,
    0x15,
    0x04,
    0xC7,
    0x23,
    0xC3,
    0x18,
    0x96,
    0x05,
    0x9A,
    0x07,
    0x12,
    0x80,
    0xE2,
    0xEB,
    0x27,
    0xB2,
    0x75,
    0x09,
    0x83,
    0x2C,
    0x1A,
    0x1B,
    0x6E,
    0x5A,
    0xA0,
    0x52,
    0x3B,
    0xD6,
    0xB3,
    0x29,
    0xE3,
    0x2F,
    0x84,
    0x53,
    0xD1,
    0x00,
    0xED,
    0x20,
    0xFC,
    0xB1,
    0x5B,
    0x6A,
    0xCB,
    0xBE,
    0x39,
    0x4A,
    0x4C,
    0x58,
    0xCF,
    0xD0,
    0xEF,
    0xAA,
    0xFB,
    0x43,
    0x4D,
    0x33,
    0x85,
    0x45,
    0xF9,
    0x02,
    0x7F,
    0x50,
    0x3C,
    0x9F,
    0xA8,
    0x51,
    0xA3,
    0x40,
    0x8F,
    0x92,
    0x9D,
    0x38,
    0xF5,
    0xBC,
    0xB6,
    0xDA,
    0x21,
    0x10,
    0xFF,
    0xF3,
    0xD2,
    0xCD,
    0x0C,
    0x13,
    0xEC,
    0x5F,
    0x97,
    0x44,
    0x17,
    0xC4,
    0xA7,
    0x7E,
    0x3D,
    0x64,
    0x5D,
    0x19,
    0x73,
    0x60,
    0x81,
    0x4F,
    0xDC,
    0x22,
    0x2A,
    0x90,
    0x88,
    0x46,
    0xEE,
    0xB8,
    0x14,
    0xDE,
    0x5E,
    0x0B,
    0xDB,
    0xE0,
    0x32,
    0x3A,
    0x0A,
    0x49,
    0x06,
    0x24,
    0x5C,
    0xC2,
    0xD3,
    0xAC,
    0x62,
    0x91,
    0x95,
    0xE4,
    0x79,
    0xE7,
    0xC8,
    0x37,
    0x6D,
    0x8D,
    0xD5,
    0x4E,
    0xA9,
    0x6C,
    0x56,
    0xF4,
    0xEA,
    0x65,
    0x7A,
    0xAE,
    0x08,
    0xBA,
    0x78,
    0x25,
    0x2E,
    0x1C,
    0xA6,
    0xB4,
    0xC6,
    0xE8,
    0xDD,
    0x74,
    0x1F,
    0x4B,
    0xBD,
    0x8B,
    0x8A,
    0x70,
    0x3E,
    0xB5,
    0x66,
    0x48,
    0x03,
    0xF6,
    0x0E,
    0x61,
    0x35,
    0x57,
    0xB9,
    0x86,
    0xC1,
    0x1D,
    0x9E,
    0xE1,
    0xF8,
    0x98,
    0x11,
    0x69,
    0xD9,
    0x8E,
    0x94,
    0x9B,
    0x1E,
    0x87,
    0xE9,
    0xCE,
    0x55,
    0x28,
    0xDF,
    0x8C,
    0xA1,
    0x89,
    0x0D,
    0xBF,
    0xE6,
    0x42,
    0x68,
    0x41,
    0x99,
    0x2D,
    0x0F,
    0xB0,
    0x54,
    0xBB,
    0x16,
)

_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_SBOX = tuple(_INV_SBOX)

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)

_NR = 10


def _mul(a: int, b: int) -> int:
    """Multiply two bytes in GF(2^8) modulo the AES polynomial 0x11B."""
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return result


def _expand_key(key: bytes) -> list[bytes]:
    """AES-128 key schedule -> 11 round keys of 16 bytes each."""
    if len(key) != 16:
        raise ValueError(f"AES-128 requires a 16-byte key, got {len(key)}")

    words: list[list[int]] = [list(key[i * 4 : i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])

    return [bytes(b for w in words[r * 4 : r * 4 + 4] for b in w) for r in range(_NR + 1)]


def _add_round_key(state: list[int], round_key: bytes) -> None:
    for i in range(16):
        state[i] ^= round_key[i]


def _inv_shift_rows(state: list[int]) -> None:

    for r in range(1, 4):
        row = [state[r + 4 * c] for c in range(4)]
        row = row[-r:] + row[:-r]
        for c in range(4):
            state[r + 4 * c] = row[c]


def _inv_mix_columns(state: list[int]) -> None:
    for c in range(4):
        a = state[4 * c : 4 * c + 4]
        state[4 * c + 0] = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
        state[4 * c + 1] = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
        state[4 * c + 2] = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
        state[4 * c + 3] = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)


def decrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    """Inverse cipher for one 16-byte block (FIPS-197 InvCipher)."""
    if len(block) != 16:
        raise ValueError(f"AES block must be 16 bytes, got {len(block)}")

    state = list(block)
    _add_round_key(state, round_keys[_NR])

    for rnd in range(_NR - 1, 0, -1):
        _inv_shift_rows(state)
        state = [_INV_SBOX[b] for b in state]
        _add_round_key(state, round_keys[rnd])
        _inv_mix_columns(state)

    _inv_shift_rows(state)
    state = [_INV_SBOX[b] for b in state]
    _add_round_key(state, round_keys[0])
    return bytes(state)


def aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC decrypt. The ciphertext length must be a multiple of 16."""
    if len(ciphertext) == 0 or len(ciphertext) % 16:
        raise ValueError(
            f"ciphertext length must be a non-zero multiple of 16, got {len(ciphertext)}"
        )
    if len(iv) < 16:
        raise ValueError(f"IV must be at least 16 bytes, got {len(iv)}")

    iv_block = iv[:16]
    round_keys = _expand_key(key)

    out = bytearray()
    prev = iv_block
    for offset in range(0, len(ciphertext), 16):
        chunk = ciphertext[offset : offset + 16]
        plain = decrypt_block(chunk, round_keys)
        out.extend(a ^ b for a, b in zip(plain, prev, strict=True))
        prev = chunk
    return bytes(out)


def strip_pkcs7(data: bytes) -> bytes:
    """Remove PKCS#7 padding, tolerating (and ignoring) a malformed pad."""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data
