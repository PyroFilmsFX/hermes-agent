"""G-2: pure Ed25519 verify against RFC 8032 test vectors and cryptography cross-check.

Pure-Python Ed25519 VERIFY (signing only as a test helper if needed),
with the RFC 8032 test vectors (section 7.1, at least tests 1, 2, 3 and 1024),
a cross-check against the cryptography package when it is importable (skip if not),
and rejecting non-canonical S (S >= L) and bad point encodings. (Addendum §3.1, §8.2 G-2.)
"""

import hashlib
import os

import pytest

from hermes_owner_grant import ed25519_pure

# Group order L and field prime p
L = 2**252 + 27742317777372353535851937790883648493
P = 2**255 - 19

# RFC 8032 Section 7.1 Test Vectors
TEST_1 = {
    "name": "TEST 1",
    "sk": bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"),
    "pk": bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"),
    "msg": b"",
    "sig": bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
}

TEST_2 = {
    "name": "TEST 2",
    "sk": bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"),
    "pk": bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"),
    "msg": bytes.fromhex("72"),
    "sig": bytes.fromhex("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
}

TEST_3 = {
    "name": "TEST 3",
    "sk": bytes.fromhex("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7"),
    "pk": bytes.fromhex("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025"),
    "msg": bytes.fromhex("af82"),
    "sig": bytes.fromhex("6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
}

TEST_1024_MSG_HEX = (
    "08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98"
    "fa6e264bf09efe12ee50f8f54e9f77b1e355f6c50544e23fb1433ddf73be84d8"
    "79de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37c93a1b270b20329d"
    "658675fc6ea534e0810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc"
    "1a858efcb8550ee3a5e1998bd177e93a7363c344fe6b199ee5d02e82d522c4fe"
    "ba15452f80288a821a579116ec6dad2b3b310da903401aa62100ab5d1a36553e"
    "06203b33890cc9b832f79ef80560ccb9a39ce767967ed628c6ad573cb116dbef"
    "efd75499da96bd68a8a97b928a8bbc103b6621fcde2beca1231d206be6cd9ec7"
    "aff6f6c94fcd7204ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed1"
    "85ce81bd84359d44254d95629e9855a94a7c1958d1f8ada5d0532ed8a5aa3fb2"
    "d17ba70eb6248e594e1a2297acbbb39d502f1a8c6eb6f1ce22b3de1a1f40cc24"
    "554119a831a9aad6079cad88425de6bde1a9187ebb6092cf67bf2b13fd65f270"
    "88d78b7e883c8759d2c4f5c65adb7553878ad575f9fad878e80a0c9ba63bcbcc"
    "2732e69485bbc9c90bfbd62481d9089beccf80cfe2df16a2cf65bd92dd597b07"
    "07e0917af48bbb75fed413d238f5555a7a569d80c3414a8d0859dc65a46128ba"
    "b27af87a71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51a"
    "ddd3b75a2b1ae0784504df543af8969be3ea7082ff7fc9888c144da2af58429e"
    "c96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9b47acdb7"
    "51fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c"
    "42f58c30c04aafdb038dda0847dd988dcda6f3bfd15c4b4c4525004aa06eeff8"
    "ca61783aacec57fb3d1f92b0fe2fd1a85f6724517b65e614ad6808d6f6ee34df"
    "f7310fdc82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08"
    "d78ff4ef5d63653a65040cf9bfd4aca7984a74d37145986780fc0b16ac451649"
    "de6188a7dbdf191f64b5fc5e2ab47b57f7f7276cd419c17a3ca8e1b939ae49e4"
    "88acba6b965610b5480109c8b17b80e1b7b750dfc7598d5d5011fd2dcc5600a3"
    "2ef5b52a1ecc820e308aa342721aac0943bf6686b64b2579376504ccc493d97e"
    "6aed3fb0f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5f"
    "b93246f6b1116398a346f1a641f3b041e989f7914f90cc2c7fff357876e506b5"
    "0d334ba77c225bc307ba537152f3f1610e4eafe595f6d9d90d11faa933a15ef1"
    "369546868a7f3a45a96768d40fd9d03412c091c6315cf4fde7cb68606937380d"
    "b2eaaa707b4c4185c32eddcdd306705e4dc1ffc872eeee475a64dfac86aba41c"
    "0618983f8741c5ef68d3a101e8a3b8cac60c905c15fc910840b94c00a0b9d0"
)

TEST_1024 = {
    "name": "TEST 1024",
    "sk": bytes.fromhex("f5e5767cf153319517630f226876b86c8160cc583bc013744c6bf255f5cc0ee5"),
    "pk": bytes.fromhex("278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e"),
    "msg": bytes.fromhex(TEST_1024_MSG_HEX),
    "sig": bytes.fromhex("0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350aa5371b1508f9f4528ecea23c436d94b5e8fcd4f681e30a6ac00a9704a188a03"),
}

RFC_VECTORS = [TEST_1, TEST_2, TEST_3, TEST_1024]


@pytest.mark.parametrize("vec", RFC_VECTORS, ids=lambda v: v["name"])
def test_rfc_8032_vectors_pass(vec):
    """The 4 required RFC 8032 test vectors verify cleanly."""
    assert ed25519_pure.verify(vec["pk"], vec["msg"], vec["sig"]) is True


@pytest.mark.parametrize("vec", RFC_VECTORS, ids=lambda v: v["name"])
def test_rfc_8032_tampered_message_fails(vec):
    """Flipping a bit in the message causes verify to reject."""
    tampered = vec["msg"] + b"\x01" if not vec["msg"] else (
        bytes([vec["msg"][0] ^ 1]) + vec["msg"][1:]
    )
    assert ed25519_pure.verify(vec["pk"], tampered, vec["sig"]) is False


@pytest.mark.parametrize("vec", RFC_VECTORS, ids=lambda v: v["name"])
def test_rfc_8032_tampered_signature_fails(vec):
    """Flipping a bit in R or S causes verify to reject."""
    # Flip bit in R
    sig_bad_r = bytes([vec["sig"][0] ^ 1]) + vec["sig"][1:]
    assert ed25519_pure.verify(vec["pk"], vec["msg"], sig_bad_r) is False
    # Flip bit in S
    sig_bad_s = vec["sig"][:63] + bytes([vec["sig"][63] ^ 1])
    assert ed25519_pure.verify(vec["pk"], vec["msg"], sig_bad_s) is False


@pytest.mark.parametrize("vec", RFC_VECTORS, ids=lambda v: v["name"])
def test_rfc_8032_tampered_public_key_fails(vec):
    """Flipping a bit in the public key causes verify to reject."""
    bad_pk = bytes([vec["pk"][0] ^ 1]) + vec["pk"][1:]
    assert ed25519_pure.verify(bad_pk, vec["msg"], vec["sig"]) is False


def test_reject_non_canonical_s():
    """Signatures with S >= L must be rejected strictly (RFC 8032 Section 5.1.7)."""
    vec = TEST_1
    pk = vec["pk"]
    msg = vec["msg"]
    r_bytes = vec["sig"][:32]
    s_valid = int.from_bytes(vec["sig"][32:], "little")

    # S = L
    sig_s_eq_l = r_bytes + L.to_bytes(32, "little")
    assert ed25519_pure.verify(pk, msg, sig_s_eq_l) is False

    # S = L + 1
    sig_s_gt_l = r_bytes + (L + 1).to_bytes(32, "little")
    assert ed25519_pure.verify(pk, msg, sig_s_gt_l) is False

    # S = 2^255 - 1
    sig_s_max = r_bytes + ((1 << 255) - 1).to_bytes(32, "little")
    assert ed25519_pure.verify(pk, msg, sig_s_max) is False

    # S = s_valid + L (if fits in 32 bytes)
    s_plus_l = s_valid + L
    if s_plus_l < (1 << 256):
        sig_s_malleable = r_bytes + s_plus_l.to_bytes(32, "little")
        assert ed25519_pure.verify(pk, msg, sig_s_malleable) is False


def test_reject_bad_point_encodings():
    """Bad point encodings in pubkey or R must be rejected (RFC 8032 Section 5.1.7)."""
    vec = TEST_1
    pk = vec["pk"]
    msg = vec["msg"]
    sig = vec["sig"]

    # y >= p in public key (non-canonical field element)
    for bad_y in [P, P + 1, P + 18, (1 << 255) - 1]:
        bad_pk = bad_y.to_bytes(32, "little")
        assert ed25519_pure.verify(bad_pk, msg, sig) is False

    # y >= p in signature R
    for bad_y in [P, P + 1, P + 18, (1 << 255) - 1]:
        bad_sig = bad_y.to_bytes(32, "little") + sig[32:]
        assert ed25519_pure.verify(pk, msg, bad_sig) is False

    # Wrong length inputs
    assert ed25519_pure.verify(pk[:31], msg, sig) is False
    assert ed25519_pure.verify(pk + b"\x00", msg, sig) is False
    assert ed25519_pure.verify(pk, msg, sig[:63]) is False
    assert ed25519_pure.verify(pk, msg, sig + b"\x00") is False

    # Point not on curve (y = 2 is not on Edwards25519)
    # Check y=2 with sign bit 0 and 1
    not_on_curve_pk_0 = (2).to_bytes(32, "little")
    not_on_curve_pk_1 = (2 | (1 << 255)).to_bytes(32, "little")
    assert ed25519_pure.verify(not_on_curve_pk_0, msg, sig) is False
    assert ed25519_pure.verify(not_on_curve_pk_1, msg, sig) is False


def test_cryptography_cross_check_200_keys():
    """Cross-check pure Ed25519 against cryptography package on 200 random keys."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        pytest.skip("cryptography not installed")

    import random
    rng = random.Random(0x0602026)  # Deterministic seed for reproducible testing

    for i in range(200):
        # Generate random message (0 to 128 bytes)
        msg_len = rng.randint(0, 128)
        msg = rng.randbytes(msg_len)

        # Generate key with cryptography
        sk = Ed25519PrivateKey.generate()
        pk = sk.public_key()
        pub = pk.public_bytes_raw()
        sig = sk.sign(msg)

        # 1. Valid signature check
        assert ed25519_pure.verify(pub, msg, sig) is True
        pk.verify(sig, msg)  # does not raise

        # 2. Tampered message check
        tampered_msg = msg + b"x" if not msg else bytes([msg[0] ^ 1]) + msg[1:]
        assert ed25519_pure.verify(pub, tampered_msg, sig) is False
        with pytest.raises(InvalidSignature):
            pk.verify(sig, tampered_msg)

        # 3. Tampered signature check
        tampered_sig = bytes([sig[0] ^ 1]) + sig[1:]
        assert ed25519_pure.verify(pub, msg, tampered_sig) is False
        with pytest.raises(InvalidSignature):
            pk.verify(tampered_sig, msg)

        # 4. S >= L check
        s = int.from_bytes(sig[32:], "little")
        s_bad = s + L
        if s_bad < (1 << 256):
            sig_s_bad = sig[:32] + s_bad.to_bytes(32, "little")
            assert ed25519_pure.verify(pub, msg, sig_s_bad) is False
            with pytest.raises(InvalidSignature):
                pk.verify(sig_s_bad, msg)

        # 5. Non-canonical point check on public key
        bad_y = (P + (rng.randint(0, 18))).to_bytes(32, "little")
        assert ed25519_pure.verify(bad_y, msg, sig) is False
