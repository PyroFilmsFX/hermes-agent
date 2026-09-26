"""HE-SECRET-HYGIENE S1: ingest masking core (detector + placeholder).

Every secret below is a FAKE built at runtime from a seeded PRNG, so no
credential-shaped literal sits in the source. The tag key is the fixed
``b"\\x11" * 32`` injected through ``StaticTagKeyProvider``; nothing here
touches the Keychain or a real key file.
"""

from __future__ import annotations

import random
import re
import string
import time

import pytest

from agent import redact
from agent.secret_hygiene import (
    PLACEHOLDER_RE,
    FileTagKeyProvider,
    SecretHygieneConfig,
    StaticTagKeyProvider,
    mask_secrets_for_ingest,
    scan_secrets,
)

_ALNUM = string.ascii_letters + string.digits
FIXED_KEY = b"\x11" * 32


def fake(n: int, seed: int, alphabet: str = _ALNUM) -> str:
    """Deterministic fake secret body with at least one lower, upper and digit."""
    rng = random.Random(seed)
    while True:
        value = "".join(rng.choice(alphabet) for _ in range(n))
        if alphabet != _ALNUM or (re.search("[a-z]", value) and re.search("[A-Z]", value)
                                  and re.search("[0-9]", value)):
            return value


@pytest.fixture
def fixed_tag_key():
    return StaticTagKeyProvider(FIXED_KEY)


@pytest.fixture
def cfg():
    return SecretHygieneConfig()


def _mask(text, provider, cfg=None):
    return mask_secrets_for_ingest(text, config=cfg or SecretHygieneConfig(), key_provider=provider)


def _kinds(findings):
    return {f.kind: f.count for f in findings}


# ---------------------------------------------------------------------------
# §1 leaks that must now be masked
# ---------------------------------------------------------------------------

def test_asyncpg_scheme_password_masked(fixed_tag_key):
    pw = fake(20, 1)
    text = f"DATABASE_URL=postgresql+asyncpg://app:{pw}@db.internal:5432/app"
    masked, findings = _mask(text, fixed_tag_key)
    assert pw not in masked
    assert "postgresql+asyncpg://app:[REDACTED:postgres-url:" in masked
    assert masked.endswith("]@db.internal:5432/app")
    assert _kinds(findings) == {"postgres-url": 1}


def test_empty_user_db_url_masked(fixed_tag_key):
    pw = fake(18, 2)
    masked, findings = _mask(f"redis at postgres://:{pw}@cache.internal/0 now", fixed_tag_key)
    assert pw not in masked
    assert "postgres://:[REDACTED:postgres-url:" in masked
    assert "@cache.internal/0 now" in masked


def test_libpq_dsn_password_masked(fixed_tag_key):
    pw = fake(16, 3)
    text = f"conn = psycopg.connect('host=db.internal port=5432 dbname=app user=app password={pw} sslmode=require')"
    masked, findings = _mask(text, fixed_tag_key)
    assert pw not in masked
    assert "password=[REDACTED:db-password:" in masked
    assert "sslmode=require" in masked and "host=db.internal" in masked
    assert _kinds(findings) == {"db-password": 1}


def test_libpq_quoted_password_masked(fixed_tag_key):
    pw = fake(14, 31)
    masked, _ = _mask(f"host=db user=app password='{pw}' dbname=app", fixed_tag_key)
    assert pw not in masked
    assert "password='[REDACTED:db-password:" in masked


def test_jdbc_password_param_masked(fixed_tag_key):
    pw = fake(15, 4)
    text = f"url: jdbc:postgresql://db.internal:5432/app?user=app&password={pw}&ssl=true"
    masked, findings = _mask(text, fixed_tag_key)
    assert pw not in masked
    assert "&password=[REDACTED:db-password:" in masked and "&ssl=true" in masked
    assert _kinds(findings) == {"db-password": 1}


def test_jdbc_sqlserver_semicolon_password_masked(fixed_tag_key):
    pw = fake(15, 41)
    masked, _ = _mask(f"jdbc:sqlserver://db:1433;databaseName=app;user=sa;password={pw};encrypt=true", fixed_tag_key)
    assert pw not in masked and ";encrypt=true" in masked


def test_pgpassword_env_masked(fixed_tag_key):
    # A human password: short, not opaque. Only the key name says it is a secret.
    masked, findings = _mask("PGPASSWORD=hunter2wins psql -h db.internal -U app", fixed_tag_key)
    assert "hunter2wins" not in masked
    assert masked.startswith("PGPASSWORD=[REDACTED:db-password:")
    assert masked.endswith("] psql -h db.internal -U app")


def test_pgpassword_and_mytoken_log_redaction_fixed():
    """The word-boundary gate bug (redact.py:345-372 vs the :219-225 comment) is fixed for logs too."""
    out = redact.redact_sensitive_text("PGPASSWORD=hunter2wins psql", force=True)
    assert "hunter2wins" not in out
    tok = fake(24, 5)
    assert tok not in redact.redact_sensitive_text(f"MYTOKEN={tok}", force=True)
    # ...without reopening the prose false positives the gate exists for.
    for text in ("KEYBOARD=notsecret", "PASSAGE=notsecret", "PASSWORDLESS=enabled", "SECRETARY=jsmith"):
        assert redact.redact_sensitive_text(text, force=True) == text


def test_flyv1_macaroon_chain_masked(fixed_tag_key):
    body1, body2 = fake(60, 6, _ALNUM + "+/"), fake(48, 7, _ALNUM + "+/")
    chain = f"FlyV1 fm2_{body1}==,fm2_{body2}"
    for text in (f"fly token is {chain} ok", f'export FLY_API_TOKEN="{chain}"'):
        masked, findings = _mask(text, fixed_tag_key)
        assert body1 not in masked and body2 not in masked, masked
        assert "fly-token" in _kinds(findings)
        assert "fm2_" not in masked


def test_bare_fm2_and_fo1_fly_tokens_masked(fixed_tag_key):
    fm2, fo1 = fake(40, 8, _ALNUM + "_-"), fake(43, 9, _ALNUM + "_-")
    masked, findings = _mask(f"deploy with fm2_{fm2} or the org token fo1_{fo1}.", fixed_tag_key)
    assert fm2 not in masked and fo1 not in masked
    assert _kinds(findings) == {"fly-token": 2}
    assert masked.endswith("].")


def test_neon_npg_password_masked(fixed_tag_key):
    pw = fake(12, 10)
    masked, findings = _mask(f"neon password: npg_{pw}", fixed_tag_key)
    assert f"npg_{pw}" not in masked
    assert _kinds(findings) == {"db-password": 1}


@pytest.mark.parametrize("text", [
    "fo1_short", "npg_abc123", "fm2_tooShort1", "the npg_ prefix", "see fo1_ docs",
])
def test_short_fly_and_neon_prefixes_not_masked(fixed_tag_key, text):
    masked, findings = _mask(text, fixed_tag_key)
    assert masked == text and not findings


def test_asia_sts_key_masked(fixed_tag_key):
    key_id = "ASIA" + fake(16, 11, string.ascii_uppercase + string.digits)
    masked, findings = _mask(f"aws_access_key_id = {key_id}", fixed_tag_key)
    assert key_id not in masked
    assert "aws-access-key" in _kinds(findings)
    assert key_id not in redact.redact_sensitive_text(f"id {key_id}", force=True)


def test_generic_high_entropy_assignment_masked(fixed_tag_key):
    value = fake(32, 12)
    masked, findings = _mask(f"neon_admin = {value}\n", fixed_tag_key)
    assert value not in masked
    assert _kinds(findings) == {"high-entropy": 1}
    assert masked.startswith("neon_admin = [REDACTED:high-entropy:")


def test_neon_url_masks_password_only_with_neon_kind(fixed_tag_key):
    pw = "npg_" + fake(12, 13)
    host = "ep-cool-darkness-123456-pooler.us-east-2.aws.neon.tech"
    text = f"postgresql://neondb_owner:{pw}@{host}/neondb?sslmode=require"
    masked, findings = _mask(text, fixed_tag_key)
    assert pw not in masked
    assert re.fullmatch(
        rf"postgresql://neondb_owner:\[REDACTED:neon-url:[0-9a-f]{{8}}\]@{re.escape(host)}/neondb\?sslmode=require",
        masked)
    assert _kinds(findings) == {"neon-url": 1}


def test_db_url_whole_url_mode(fixed_tag_key):
    pw = fake(16, 14)
    masked, _ = _mask(f"use postgres://u:{pw}@h/db please",
                      fixed_tag_key, SecretHygieneConfig(db_url_mask="whole_url"))
    assert re.fullmatch(r"use \[REDACTED:postgres-url:[0-9a-f]{8}\] please", masked)


def test_sk_ant_full_replacement_no_head_tail_chars(fixed_tag_key):
    key = "sk-ant-api03-" + fake(80, 15, _ALNUM + "_-")
    masked, findings = _mask(f"key {key} end", fixed_tag_key)
    assert re.fullmatch(r"key \[REDACTED:anthropic-key:[0-9a-f]{8}\] end", masked)
    assert key[6:12] not in masked and key[-4:] not in masked
    assert _kinds(findings) == {"anthropic-key": 1}


# ---------------------------------------------------------------------------
# Tag properties
# ---------------------------------------------------------------------------

def _tag(masked):
    return PLACEHOLDER_RE.search(masked).group("tag")


def test_same_secret_same_tag(fixed_tag_key):
    key = "ghp_" + fake(36, 16)
    a, _ = _mask(f"first {key}", fixed_tag_key)
    b, _ = _mask(f"TOKEN: {key} second time", fixed_tag_key)
    assert _tag(a) == _tag(b)
    assert len(_tag(a)) == 8


def test_different_secret_different_tag(fixed_tag_key):
    a, _ = _mask("ghp_" + fake(36, 17), fixed_tag_key)
    b, _ = _mask("ghp_" + fake(36, 18), fixed_tag_key)
    assert _tag(a) != _tag(b)


def test_tag_depends_on_key():
    key = "ghp_" + fake(36, 19)
    a, _ = _mask(key, StaticTagKeyProvider(b"\x11" * 32))
    b, _ = _mask(key, StaticTagKeyProvider(b"\x22" * 32))
    assert _tag(a) != _tag(b)


def test_mask_idempotent(fixed_tag_key):
    corpus = "\n".join([
        f"postgresql://neondb_owner:npg_{fake(12, 20)}@ep-x.neon.tech/neondb?sslmode=require",
        f"PGPASSWORD={fake(10, 21)} psql",
        f"export OPENAI_API_KEY=sk-proj-{fake(40, 22)}",
        f"Authorization: Bearer {fake(40, 23)}",
        f"FlyV1 fm2_{fake(50, 24)},fm2_{fake(50, 25)}",
        f'{{"password": "{fake(12, 26)}", "api_key": "{fake(30, 27)}"}}',
        f"host=db user=app password={fake(12, 28)} dbname=x",
        f"jdbc:mysql://db/app?password={fake(12, 29)}",
        f"random_setting: {fake(30, 30)}",
        "-----BEGIN OPENSSH PRIVATE KEY-----\n" + fake(64, 31) + "\n-----END OPENSSH PRIVATE KEY-----",
        f"AKIA{fake(16, 32, string.ascii_uppercase + string.digits)}",
        f"eyJ{fake(20, 33)}.{fake(20, 34)}.{fake(20, 35)}",
    ])
    once, findings = _mask(corpus, fixed_tag_key)
    assert sum(f.count for f in findings) >= 12
    twice, again = _mask(once, fixed_tag_key)
    assert twice == once
    assert again == ()
    assert scan_secrets(once, config=SecretHygieneConfig()) == ()


def test_findings_carry_kind_count_span_never_value(fixed_tag_key):
    pw = fake(20, 36)
    text = f"x postgres://u:{pw}@h/db y"
    _, findings = _mask(text, fixed_tag_key)
    (finding,) = findings
    assert finding.kind == "postgres-url" and finding.count == 1
    (start, end), = finding.spans
    assert text[start:end] == pw
    assert pw not in repr(finding) and pw not in str(finding)
    assert set(vars(finding)) == {"kind", "count", "spans"}


# ---------------------------------------------------------------------------
# §4 false-positive control
# ---------------------------------------------------------------------------

_PY_SAMPLE = "\n".join(
    ["import os", "MAX_TOKENS=100", "TEMPERATURE = 0.7", "model = 'claude-3-5-sonnet-20241022'",
     "api_key = os.environ['OPENAI_API_KEY']", "token_count = len(tokens)",
     "def handler(event, context):", "    session_id = event['session']", "    return {'ok': True}"]
    + [f"step_{i} = compute_value(step_{i - 1}, factor={i})" for i in range(1, 32)]
)


@pytest.mark.parametrize("text", [
    "id: 123e4567-e89b-12d3-a456-426614174000",
    "commit 3f2a9c1 fixes it",
    "sha: 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39",
    "digest = 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    'integrity: "sha512-z4PhNX7vuL3xVChQ1m2AB9Yg5AULVxXcg/SpIdNs6c5H0NE8XYXysP+DGNKHfuwvY7kxvUdBeoGlODJ6+SfaPg=="',
    "image: data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
    "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUQ3ZbC8kq9Xn2Yl4hQeM0xWbT3m8wCgYIKoZIzj0EAwIw\n-----END CERTIFICATE-----",
    "API_KEY=your-api-key-here",
    "DATABASE_URL=${DATABASE_URL}",
    'dsn = f"postgresql://{user}:{pw}@{host}/db"',
    "password: ***",
    "token: [REDACTED:github-token:0a1b2c3d]",
    "The quick brown fox jumps over the lazy dog while the committee reviews credentials policy.",
    _PY_SAMPLE,
])
def test_no_false_positive(fixed_tag_key, text):
    masked, findings = _mask(text, fixed_tag_key)
    assert findings == ()
    assert masked == text


def test_code_fence_known_prefix_still_masked_entropy_not(fixed_tag_key):
    real = "sk-ant-api03-" + fake(60, 40, _ALNUM + "_-")
    blob = fake(32, 41)
    text = ("Here is my config:\n```python\n"
            'API_KEY = "test"\n'
            f"CLIENT = Anthropic(api_key=\"{real}\")\n"
            f"fixture_digest = {blob}\n"
            "```\n")
    masked, findings = _mask(text, fixed_tag_key)
    assert real not in masked
    assert 'API_KEY = "test"' in masked
    assert f"fixture_digest = {blob}" in masked
    assert _kinds(findings) == {"anthropic-key": 1}
    # The same entropy assignment OUTSIDE a fence is masked.
    masked_out, _ = _mask(f"fixture_digest = {blob}", fixed_tag_key)
    assert blob not in masked_out


def test_keyword_assignment_in_fence_needs_entropy(fixed_tag_key):
    strong = fake(32, 42)
    text = f"```\nDB_PASSWORD=changeme\nSERVICE_TOKEN={strong}\n```"
    masked, _ = _mask(text, fixed_tag_key)
    assert "DB_PASSWORD=changeme" in masked
    assert strong not in masked


def test_bare_high_entropy_string_not_masked_by_default(fixed_tag_key):
    text = f"the id is {fake(32, 43)} for reference"
    assert _mask(text, fixed_tag_key) == (text, ())


def test_user_allow_list(fixed_tag_key):
    import hashlib
    value = fake(32, 44)
    cfg = SecretHygieneConfig(allow_value_sha256=(hashlib.sha256(value.encode()).hexdigest(),))
    assert _mask(f"setting = {value}", fixed_tag_key, cfg)[0] == f"setting = {value}"
    cfg2 = SecretHygieneConfig(allow_patterns=("^" + value[:6],))
    assert _mask(f"setting = {value}", fixed_tag_key, cfg2)[0] == f"setting = {value}"


def test_disabled_config_is_passthrough(fixed_tag_key):
    text = "ghp_" + fake(36, 45)
    assert _mask(text, fixed_tag_key, SecretHygieneConfig(enabled=False)) == (text, ())


def test_log_format_unchanged_for_prefix_tokens():
    key = "sk-proj-" + fake(40, 46)
    out = redact.redact_sensitive_text(f"key {key}", force=True)
    assert out == f"key {key[:6]}...{key[-4:]}"


# ---------------------------------------------------------------------------
# Structural gates and performance
# ---------------------------------------------------------------------------

def test_new_patterns_pass_redos_gate():
    from agent.redact_detect import NEW_PATTERN_SOURCES

    assert NEW_PATTERN_SOURCES, "the detector must expose its new regex sources"
    for pattern in NEW_PATTERN_SOURCES:
        assert not redact._has_nested_unbounded_repeat(pattern), pattern
    for pattern in redact._PREFIX_PATTERNS:
        assert not redact._has_nested_unbounded_repeat(pattern), pattern
        assert not redact._has_top_level_alternation(pattern), pattern
        assert len(redact._extract_literal_prefix(pattern)) >= 2, pattern


def test_perf_30kb_paste_under_budget(fixed_tag_key):
    rng = random.Random(47)
    words = ["deploy", "the", "service", "config", "value", "retry", "timeout=30", "host: db", "ok"]
    lines = [" ".join(rng.choice(words) for _ in range(12)) for _ in range(400)]
    lines[50] = f"postgresql://u:{fake(20, 48)}@h/db"
    lines[300] = f"export GITHUB_TOKEN=ghp_{fake(36, 49)}"
    text = "\n".join(lines)
    text += "\n" + fake(4000, 50, _ALNUM + "+/")  # an unbroken blob
    assert len(text) >= 30_000
    _mask(text, fixed_tag_key)  # warm regex caches
    start = time.perf_counter()
    masked, findings = _mask(text, fixed_tag_key)
    elapsed = time.perf_counter() - start
    assert sum(f.count for f in findings) == 2
    assert elapsed < 0.25, f"30 KB paste took {elapsed * 1000:.1f} ms"


# ---------------------------------------------------------------------------
# Key source (§3.1): config.yaml + key file, never env vars
# ---------------------------------------------------------------------------

def test_file_tag_key_provider_creates_owner_only_key(tmp_path):
    provider = FileTagKeyProvider(tmp_path / "secret-reveal", use_keychain=False)
    key = provider.get_key()
    assert len(key) == 32
    key_file = tmp_path / "secret-reveal" / "tag.key"
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "secret-reveal").stat().st_mode & 0o777 == 0o700
    assert FileTagKeyProvider(tmp_path / "secret-reveal", use_keychain=False).get_key() == key


def test_config_read_from_config_yaml(monkeypatch):
    import agent.secret_hygiene as sh

    monkeypatch.setattr(sh, "_load_security_section", lambda: {
        "secret_hygiene": {"db_url_mask": "whole_url", "tag_hex_chars": 12,
                           "entropy": {"min_length": 30}}})
    loaded = sh.load_secret_hygiene_config()
    assert loaded.db_url_mask == "whole_url"
    assert loaded.tag_hex_chars == 12
    assert loaded.entropy_min_length == 30
    assert loaded.entropy_min_bits == 4.0


def test_config_defaults_registered():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    block = DEFAULT_CONFIG["security"]["secret_hygiene"]
    assert block["enabled"] is True and block["db_url_mask"] == "password"
    assert block["entropy"]["min_bits_per_char"] == 4.0


# ---------------------------------------------------------------------------
# Structured JSON: ids are not secrets (security review P1-6)
# ---------------------------------------------------------------------------

def test_json_id_keys_keep_tool_call_session_and_uuid_values(fixed_tag_key):
    import json

    from agent.secret_hygiene import mask_json_value, mask_stored_text

    doc = {
        "id": "call_cfedFhJjGmu1RvRc1OUC38j8",
        "tool_call_id": "call_" + fake(24, 801),
        "call_id": "toolu_01" + fake(22, 802),
        "session_id": "20260926_101010_" + fake(24, 803),
        "sessionId": "sess_" + fake(28, 804),
        "uuid": "5b0c1d2e-0000-4000-8000-00000000abcd",
        "parentUuid": "7c0c1d2e-0000-4000-8000-00000000abcd",
        "tool_calls": [{"id": "call_" + fake(24, 805), "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"}}],
    }
    raw = json.dumps(doc)
    for dry_run in (True, False):
        new, counts = mask_json_value(doc, config=SecretHygieneConfig(), key_provider=fixed_tag_key,
                                      dry_run=dry_run)
        assert new == doc and not counts, counts
        text, counts = mask_stored_text(raw, config=SecretHygieneConfig(), key_provider=fixed_tag_key,
                                        dry_run=dry_run)
        assert text == raw and not counts, counts


def test_json_prefix_secret_under_an_id_key_is_still_masked(fixed_tag_key):
    from agent.secret_hygiene import mask_json_value

    secret = "sk-ant-api03-" + fake(60, 810)
    new, counts = mask_json_value({"id": secret, "tool_call_id": "call_" + fake(24, 811)},
                                  config=SecretHygieneConfig(), key_provider=fixed_tag_key)
    assert secret not in new["id"] and new["id"].startswith("[REDACTED:anthropic-key:")
    assert new["tool_call_id"].startswith("call_")
    assert counts == {"anthropic-key": 1}


def test_json_entropy_rule_only_under_secret_named_keys(fixed_tag_key):
    from agent.secret_hygiene import mask_json_value

    value = fake(32, 820)
    new, counts = mask_json_value(
        {"client_secret": value, "note": value, "request_id": value, "cursor": value,
         "content": f"cursor_pos = {value}"},
        config=SecretHygieneConfig(), key_provider=fixed_tag_key)
    assert value not in new["client_secret"]
    assert new["note"] == value and new["request_id"] == value and new["cursor"] == value
    assert new["content"] == f"cursor_pos = {value}"
    assert sum(counts.values()) == 1
