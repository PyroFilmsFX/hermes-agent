"""Pure-Python, stdlib-only Ed25519 verify (RFC 8032) with optional cryptography fast-path.

Unit U2 of the #60 owner-grant verifier (HE-OWNER-FORWARD addendum §3.1).
Rejects non-canonical S (S >= L) and non-canonical point encodings (y >= p).
"""

from __future__ import annotations

import hashlib
from typing import Optional, Tuple

# Field prime p = 2^255 - 19
P: int = 2**255 - 19

# Group order L
L: int = 2**252 + 27742317777372353535851937790883648493


def _modp_inv(x: int) -> int:
    return pow(x, P - 2, P)


# Curve constant d = -121665 / 121666 mod P
D: int = (-121665 * _modp_inv(121666)) % P

# Square root of -1 in GF(P)
SQRT_M1: int = pow(2, (P - 1) // 4, P)


def _sha512(b: bytes) -> bytes:
    return hashlib.sha512(b).digest()


def _sha512_mod_l(b: bytes) -> int:
    return int.from_bytes(_sha512(b), "little") % L


Point = Tuple[int, int, int, int]


def _point_add(p1: Point, p2: Point) -> Point:
    """Add two points in extended Edwards coordinates: (X, Y, Z, T)."""
    a = (p1[1] - p1[0]) * (p2[1] - p2[0]) % P
    b = (p1[1] + p1[0]) * (p2[1] + p2[0]) % P
    c = 2 * p1[3] * p2[3] * D % P
    d = 2 * p1[2] * p2[2] % P
    e = (b - a) % P
    f = (d - c) % P
    g = (d + c) % P
    h = (b + a) % P
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _point_mul(s: int, p_point: Point) -> Point:
    """Scalar multiplication Q = s * P in extended Edwards coordinates."""
    q: Point = (0, 1, 1, 0)  # Neutral element
    base: Point = p_point
    while s > 0:
        if s & 1:
            q = _point_add(q, base)
        base = _point_add(base, base)
        s >>= 1
    return q


def _point_equal(p1: Point, p2: Point) -> bool:
    """Check equality of two projective points: X1*Z2 == X2*Z1 and Y1*Z2 == Y2*Z1."""
    return (
        (p1[0] * p2[2] - p2[0] * p1[2]) % P == 0
        and (p1[1] * p2[2] - p2[1] * p1[2]) % P == 0
    )


def _recover_x(y: int, sign: int) -> Optional[int]:
    """Recover x coordinate given y and sign bit (x mod 2 == sign)."""
    if y >= P:
        return None
    x2 = (y * y - 1) * _modp_inv(D * y * y + 1) % P
    if x2 == 0:
        return None if sign != 0 else 0

    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = (x * SQRT_M1) % P
    if (x * x - x2) % P != 0:
        return None

    if (x & 1) != sign:
        x = P - x
    return x


# Base point G = (gx, gy, 1, gx*gy mod P)
_GY: int = (4 * _modp_inv(5)) % P
_GX: Optional[int] = _recover_x(_GY, 0)
assert _GX is not None
G: Point = (_GX, _GY, 1, (_GX * _GY) % P)


def point_decompress(s: bytes) -> Optional[Point]:
    """Decode a 32-byte point representation.

    Rejects non-canonical field elements (y >= P), invalid lengths,
    and points not on the curve.
    """
    if len(s) != 32:
        return None
    val = int.from_bytes(s, "little")
    sign = val >> 255
    y = val & ((1 << 255) - 1)
    if y >= P:
        return None
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, (x * y) % P)


def point_compress(p_point: Point) -> bytes:
    """Encode an extended point into a 32-byte representation."""
    zinv = _modp_inv(p_point[2])
    x = p_point[0] * zinv % P
    y = p_point[1] * zinv % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 signature in pure Python (RFC 8032 Section 5.1.7).

    Returns True if valid, False otherwise.
    Rejects non-canonical S (S >= L) and non-canonical point encodings.
    """
    if (
        not isinstance(public_key, (bytes, bytearray))
        or len(public_key) != 32
        or not isinstance(signature, (bytes, bytearray))
        or len(signature) != 64
        or not isinstance(message, (bytes, bytearray))
    ):
        return False

    # 1. Decode S and reject non-canonical scalar (S >= L)
    s = int.from_bytes(signature[32:], "little")
    if s >= L or s < 0:
        return False

    # 2. Decode public key point A
    a_point = point_decompress(bytes(public_key))
    if a_point is None:
        return False

    # 3. Decode R point
    r_bytes = bytes(signature[:32])
    r_point = point_decompress(r_bytes)
    if r_point is None:
        return False

    # 4. Compute k = SHA-512(R || A || M) mod L
    k = _sha512_mod_l(r_bytes + bytes(public_key) + bytes(message))

    # 5. Check group equation: [S]G == R + [k]A
    sb = _point_mul(s, G)
    ka = _point_mul(k, a_point)
    r_plus_ka = _point_add(r_point, ka)
    return _point_equal(sb, r_plus_ka)


def public_key(secret_key: bytes) -> bytes:
    """Derive 32-byte public key from 32-byte secret key (test helper)."""
    if len(secret_key) != 32:
        raise ValueError("Secret key must be 32 bytes")
    h = _sha512(secret_key)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= (1 << 254)
    return point_compress(_point_mul(a, G))


def sign(secret_key: bytes, message: bytes) -> bytes:
    """Sign a message using a 32-byte secret key (test helper)."""
    if len(secret_key) != 32:
        raise ValueError("Secret key must be 32 bytes")
    h = _sha512(secret_key)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= (1 << 254)
    prefix = h[32:]
    a_bytes = point_compress(_point_mul(a, G))
    r = _sha512_mod_l(prefix + message)
    r_point = _point_mul(r, G)
    r_bytes = point_compress(r_point)
    k = _sha512_mod_l(r_bytes + a_bytes + message)
    s = (r + k * a) % L
    return r_bytes + s.to_bytes(32, "little")


def verify_cryptography(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Optional fast-path verification via cryptography package."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except (ImportError, ModuleNotFoundError):
        return False

    if len(public_key) != 32 or len(signature) != 64:
        return False

    # Check S < L for strict RFC 8032 compliance
    s = int.from_bytes(signature[32:], "little")
    if s >= L or s < 0:
        return False

    # Check y < P for strict canonical point encoding
    raw_y = int.from_bytes(public_key, "little") & ((1 << 255) - 1)
    if raw_y >= P:
        return False
    raw_ry = int.from_bytes(signature[:32], "little") & ((1 << 255) - 1)
    if raw_ry >= P:
        return False

    try:
        pk = Ed25519PublicKey.from_public_bytes(public_key)
        pk.verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False
