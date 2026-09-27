"""G-1: the owner-grant envelope (HE-OWNER-FORWARD verify addendum §2.1).

The signature covers ``b"hermes-owner-grant/v1\\x00" + payload_bytes``; verifiers never
re-canonicalize; ``grant_id`` is derived from the payload hash and never stored, so a
relabelled file name (or an in-band id field) can't change which grant a file is.
"""

import base64
import hashlib
import json
import os

import pytest

from hermes_owner_grant import envelope

KID = "ok_0123456789abcdef"
PAYLOAD_OBJ = {
    "v": 1,
    "aud": ["hermes-owner-forward", "hermes-owner-verify"],
    "issued_at": 1790000000000,
    "expires_at": 1790043200000,
    "scope": [],
    "text": "Merge the été branch once CI is green.",
}


def _fake_signer(calls):
    """Deterministic 64-byte stand-in for Ed25519 (U2 owns the real primitive)."""

    def sign(message):
        calls.append(message)
        return hashlib.sha512(message).digest()

    return sign


def _sealed(payload=None):
    payload = envelope.encode_payload(PAYLOAD_OBJ) if payload is None else payload
    return envelope.seal(payload, KID, _fake_signer([]))


# -- domain prefix and sign bytes ---------------------------------------------------------


def test_sign_bytes_are_domain_prefix_plus_exact_payload():
    assert envelope.FORMAT == "hermes-owner-grant/v1"
    assert envelope.DOMAIN_PREFIX == b"hermes-owner-grant/v1\x00"
    payload = b'{"a":1}'
    assert envelope.sign_bytes(payload) == b"hermes-owner-grant/v1\x00" + payload


def test_seal_signs_the_prefixed_bytes_and_round_trips_exact_bytes():
    calls = []
    payload = envelope.encode_payload(PAYLOAD_OBJ)
    sealed = envelope.seal(payload, KID, _fake_signer(calls))
    assert calls == [envelope.DOMAIN_PREFIX + payload]

    wire = sealed.to_json()
    as_dict = json.loads(wire)
    assert set(as_dict) == {"format", "kid", "payload", "sig"}
    assert as_dict["format"] == "hermes-owner-grant/v1"

    for source in (wire, wire.encode("utf-8"), as_dict):
        parsed = envelope.parse_envelope(source)
        assert parsed.payload == payload  # exact bytes, no re-canonicalization
        assert parsed.sig == sealed.sig
        assert parsed.kid == KID
        assert parsed.sign_bytes() == envelope.DOMAIN_PREFIX + payload
        assert parsed.grant_id == sealed.grant_id
        assert envelope.decode_payload(parsed.payload) == PAYLOAD_OBJ


def test_non_canonical_payload_bytes_are_preserved_not_reserialized():
    # A JS signer may emit different whitespace/key order; the verifier must keep the bytes.
    payload = b'{ "v":1,  "aud":["hermes-owner-verify"] }'
    parsed = envelope.parse_envelope(_sealed(payload).to_dict())
    assert parsed.payload == payload
    assert parsed.grant_id == envelope.derive_grant_id(payload)


def test_encode_payload_is_compact_sorted_utf8():
    raw = envelope.encode_payload({"b": 1, "a": "é"})
    assert raw == '{"a":"é","b":1}'.encode("utf-8")


def test_seal_refuses_a_signer_that_returns_the_wrong_length():
    with pytest.raises(envelope.EnvelopeError):
        envelope.seal(b"{}", KID, lambda _m: b"\x00" * 63)


# -- derived grant id ---------------------------------------------------------------------


def test_grant_id_is_og_plus_lowercase_b32_sha256_prefix():
    payload = envelope.encode_payload(PAYLOAD_OBJ)
    expected = (
        "og_"
        + base64
        .b32encode(hashlib.sha256(payload).digest())
        .decode("ascii")[:26]
        .lower()
    )
    assert envelope.derive_grant_id(payload) == expected
    assert len(expected) == 29
    assert _sealed(payload).grant_id == expected


def test_grant_id_changes_with_any_payload_byte_and_ignores_the_signature():
    payload = envelope.encode_payload(PAYLOAD_OBJ)
    flipped = payload[:-2] + bytes([payload[-2] ^ 0x01]) + payload[-1:]
    assert envelope.derive_grant_id(payload) != envelope.derive_grant_id(flipped)
    a = envelope.seal(payload, KID, lambda _m: b"\x01" * 64)
    b = envelope.seal(payload, KID, lambda _m: b"\x02" * 64)
    assert a.grant_id == b.grant_id


def test_an_in_band_grant_id_field_is_refused_so_ids_cannot_be_relabelled():
    wire = _sealed().to_dict()
    wire["grant_id"] = "og_" + "a" * 26
    with pytest.raises(envelope.EnvelopeError):
        envelope.parse_envelope(wire)


def test_relabelled_file_name_is_ignored(tmp_path):
    sealed = _sealed()
    other_id = envelope.derive_grant_id(b"something else")
    path = tmp_path / envelope.grant_filename(1111, other_id)
    path.write_text(sealed.to_json(), encoding="utf-8")

    hint = envelope.parse_grant_filename(path.name)
    assert hint == (1111, other_id)  # the name is only a lookup hint ...
    loaded = envelope.read_envelope_file(str(path))
    assert (
        loaded.grant_id == sealed.grant_id != other_id
    )  # ... the id comes from the bytes
    assert loaded.payload == sealed.payload


def test_grant_filename_shape():
    gid = envelope.derive_grant_id(b"x")
    assert envelope.grant_filename(1790000000000, gid) == "1790000000000-%s.json" % gid
    for bad in (
        "x.json",
        "1790000000000-og_short.json",
        "-1-%s.json" % gid,
        "12-%s.txt" % gid,
        "12-%s.json" % gid.upper(),
        "0012-%s.json" % gid,
    ):
        assert envelope.parse_grant_filename(bad) is None


def test_read_envelope_file_refuses_non_regular_files_and_oversize(tmp_path):
    fifo = tmp_path / "1-fifo.json"
    os.mkfifo(str(fifo))
    with pytest.raises(envelope.EnvelopeError):
        envelope.read_envelope_file(str(fifo))  # must not block on a FIFO
    with pytest.raises(envelope.EnvelopeError):
        envelope.read_envelope_file(str(tmp_path))
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (envelope.MAX_ENVELOPE_BYTES + 1))
    with pytest.raises(envelope.EnvelopeError):
        envelope.read_envelope_file(str(big))
    with pytest.raises(envelope.EnvelopeError):
        envelope.read_envelope_file(str(tmp_path / "missing.json"))


# -- kid ----------------------------------------------------------------------------------


def test_kid_is_ok_plus_16_hex_of_sha256_pub():
    pub = bytes(range(32))
    assert envelope.kid_for_pub(pub) == "ok_" + hashlib.sha256(pub).hexdigest()[:16]
    with pytest.raises(envelope.EnvelopeError):
        envelope.kid_for_pub(b"\x00" * 31)


# -- b64url strictness --------------------------------------------------------------------


def test_b64url_round_trip_without_padding():
    for n in range(0, 70):
        data = os.urandom(n)
        text = envelope.b64url_encode(data)
        assert "=" not in text and "+" not in text and "/" not in text
        assert envelope.b64url_decode(text) == data


@pytest.mark.parametrize(
    "bad",
    [
        "AAE=",  # padding
        "AA+E",  # standard alphabet
        "AA/E",
        "AA E",  # whitespace is not skipped
        "AA\nE",
        "A",  # impossible length
        "AB",  # non-canonical trailing bits (canonical form is "AA")
        "éAAA",
    ],
)
def test_b64url_decode_is_strict(bad):
    with pytest.raises(envelope.EnvelopeError):
        envelope.b64url_decode(bad)


# -- envelope shape -----------------------------------------------------------------------


def _mutated(**changes):
    wire = _sealed().to_dict()
    for key, value in changes.items():
        if value is None:
            del wire[key]
        else:
            wire[key] = value
    return wire


@pytest.mark.parametrize(
    "changes",
    [
        {"format": "hermes-owner-grant/v2"},
        {"format": None},
        {"kid": "ok_0123456789ABCDEF"},
        {"kid": "ok_0123"},
        {"kid": None},
        {"sig": base64.urlsafe_b64encode(b"\x00" * 63).decode().rstrip("=")},
        {"sig": None},
        {"payload": ""},
        {"payload": None},
        {"payload": 123},
    ],
)
def test_malformed_envelopes_are_refused(changes):
    with pytest.raises(envelope.EnvelopeError) as err:
        envelope.parse_envelope(_mutated(**changes))
    assert err.value.reason == "malformed"


@pytest.mark.parametrize(
    "raw", [b"[]", b"not json", b"\xff\xfe", b'{"format":"x","format":"y"}']
)
def test_envelope_text_must_be_a_single_json_object(raw):
    with pytest.raises(envelope.EnvelopeError):
        envelope.parse_envelope(raw)


# -- payload decoding (done unverified for candidate filtering; parsed strictly) ----------


@pytest.mark.parametrize(
    "raw",
    [
        b'{"v":1,"v":2}',  # duplicate keys: parser-differential bait
        b'{"v":NaN}',
        b'{"v":Infinity}',
        b"[1]",
        b"\xff",
        b'{"v":1} trailing',
    ],
)
def test_decode_payload_is_strict(raw):
    with pytest.raises(envelope.EnvelopeError):
        envelope.decode_payload(raw)
