"""Public identifier generation shared by API write paths."""

import secrets
import time


def new_ulid() -> str:
    """Return a valid monotonic-time ULID with random entropy."""

    value = (time.time_ns() // 1_000_000 << 80) | secrets.randbits(80)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    encoded = ""
    for _ in range(26):
        encoded = alphabet[value & 31] + encoded
        value >>= 5
    return encoded
