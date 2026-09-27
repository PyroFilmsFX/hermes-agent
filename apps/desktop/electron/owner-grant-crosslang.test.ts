/**
 * #60 E-10 (E-3 style): an envelope signed by the Electron key store and signing core verifies
 * in the Python verifier `hermes_owner_grant`, and through `owner.forward`; a tampered byte is
 * refused by both. This is the byte-compatibility proof:
 * same domain prefix, same payload bytes, same kid and grant_id derivations, same file name.
 *
 * The Python side runs as a child process with an injected test anchor (never the root-owned
 * one) and a mkdtemp grants dir. safeStorage is a mock. Python defaults to /tmp/venv314 and can
 * be pointed elsewhere with OWNER_GRANT_TEST_PYTHON; the test is skipped (loudly) without one.
 */

import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, test } from 'vitest'

import { createOwnerKeyStore, type SafeStorageLike } from './owner-grant-key'
import { confirmAndSignGrants, type OwnerGrantEnvelope, type SignedOutcome, type SignRequest } from './owner-grant-sign'

const PYTHON = process.env.OWNER_GRANT_TEST_PYTHON || '/tmp/venv314/bin/python'
const REPO = path.resolve(__dirname, '../../..')
const HAVE_PYTHON = fs.existsSync(PYTHON) && fs.existsSync(path.join(REPO, 'hermes_owner_grant', 'verify.py'))

const tmpDirs: string[] = []

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

const mockSafeStorage: SafeStorageLike = {
  isEncryptionAvailable: () => true,
  encryptString: (plain: string) => Buffer.from('W:' + Buffer.from(plain).toString('base64')),
  decryptString: (wrapped: Buffer) => Buffer.from(wrapped.toString().slice(2), 'base64').toString()
}

// Runs under `python -I` (no PYTHON* env, no user site); the repo root is put on sys.path by hand.
const CHILD = String.raw`
import json, os, sys
req = json.load(sys.stdin)
OUTPUT = sys.stdout
sys.path.insert(0, req["repo"])
from hermes_owner_grant import anchor as A, envelope as E, verify as V, ed25519_pure
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

uid = os.getuid()
anchor = A.parse_anchor(json.dumps({
    "format": "hermes-owner-anchor/v1", "owner_uid": uid, "grants_dir": req["grants_dir"],
    "keys": [{"kid": req["kid"], "alg": "Ed25519", "pub": req["pub"], "status": "active",
              "not_before": 0, "retired_at": None}]}).encode("utf-8"))
pub = E.b64url_decode(req["pub"])
out = {"kid_py": E.kid_for_pub(pub), "cases": []}
for case in req["cases"]:
    env = case["envelope"]
    row = {"name": case["name"]}
    parsed = E.parse_envelope(env)
    row["grant_id_py"] = parsed.grant_id
    msg = parsed.sign_bytes()
    row["sign_bytes_prefix_ok"] = msg.startswith(b"hermes-owner-grant/v1\x00")
    row["pure_ok"] = ed25519_pure.verify(pub, msg, parsed.sig)
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(parsed.sig, msg)
        row["crypto_ok"] = True
    except InvalidSignature:
        row["crypto_ok"] = False
    try:
        row["reencode_identical"] = E.encode_payload(E.decode_payload(parsed.payload)) == parsed.payload
    except Exception as exc:
        row["reencode_identical"] = "error: %s" % exc
    q = case["query"]
    r = V.verify_envelope(env, session=q["session"], claude_session=q.get("claude_session"), uid=uid,
                          now=q["now"], text_sha=q.get("text_sha"), scopes=q.get("scopes", []),
                          subject=q.get("subject"), anchor=anchor)
    row["envelope"] = {"ok": r.ok, "reason": r.reason, "detail": r.detail,
                       "grant_id": (r.grant or {}).get("id")}
    if case.get("lookup"):
        lr = V.verify(session=q["session"], claude_session=q.get("claude_session"), uid=uid, now=q["now"],
                      text_sha=q.get("text_sha"), scopes=q.get("scopes", []), subject=q.get("subject"),
                      anchor=anchor)
        row["lookup"] = {"ok": lr.ok, "reason": lr.reason, "detail": lr.detail,
                         "grant_id": (lr.grant or {}).get("id"), "candidates": lr.candidates}
    out["cases"].append(row)
if req.get("owner_forward"):
    from tui_gateway import owner_forward as OF
    import tui_gateway.server
    from tui_gateway.transport import bind_transport, reset_transport
    # The gateway's trust root is the root-owned anchor: inject the same in-process test anchor
    # (the real loader reads /Library, which a test never touches). The backend binding is the
    # start-of-process value, passed here as the start env.
    OF._load_anchor = lambda: anchor
    start_env = {OF.BACKEND_ENV: req["owner_forward"]["backend"]}
    out["owner_forward_verifier_configured"] = OF.verifier_at_startup(start_env) is not None
    OF.policy = lambda: dict(OF._DEFAULTS)
    OF._resolve_targets = lambda requested, claims, pol, home: (
        [("w-1", "w-1", None, "default", "w-1")], "manager", None)
    OF.deliver = lambda claims, text, targets, origin_title="": [
        {"target_session_id": "w-1", "status": "delivered", "detail": None}]
    class Transport:
        session_spawn_capability = None
    params = {"envelope": req["owner_forward"]["envelope"], "text": req["owner_forward"]["text"],
              "targets": ["default:w-1"]}
    token = bind_transport(Transport())
    try:
        OF._reset_for_tests()
        OF._verifier = OF.verifier_at_startup(start_env)
        good = OF.forward_rpc("valid", params)
        bad_params = dict(params)
        bad_params["envelope"] = req["owner_forward"]["tampered"]
        bad = OF.forward_rpc("tampered", bad_params)
        OF._load_anchor = lambda: A.parse_anchor(json.dumps({
            "format": "hermes-owner-anchor/v1", "owner_uid": uid, "grants_dir": req["grants_dir"],
            "keys": [{"kid": req["kid"], "alg": "Ed25519", "pub": req["pub"], "status": "revoked",
                      "not_before": 0, "retired_at": None}]}).encode("utf-8"))
        OF._reset_for_tests()
        revoked = OF.forward_rpc("revoked", params)
        out["owner_forward"] = {"good": good, "tampered": bad, "revoked": revoked}
    finally:
        reset_transport(token)
OUTPUT.write(json.dumps(out))
`

function runPython(input: unknown) {
  const res = spawnSync(PYTHON, ['-I', '-c', CHILD], {
    input: JSON.stringify(input),
    encoding: 'utf8',
    // A clean env: no HERMES_HOME / HERMES_SESSION_ID or anything else from this shell.
    env: { PATH: '/usr/bin:/bin', LANG: 'en_US.UTF-8' },
    timeout: 60_000
  })

  if (res.status !== 0) {
    throw new Error(`python child failed (${res.status}): ${res.stderr}`)
  }

  if (!res.stdout.trim()) {
    throw new Error(`python child returned no JSON (signal=${res.signal}): ${res.stderr}`)
  }

  return JSON.parse(res.stdout)
}

function tamperPayload(env: OwnerGrantEnvelope, from: string, to: string): OwnerGrantEnvelope {
  const bytes = Buffer.from(env.payload, 'base64url').toString('utf8')
  expect(bytes.includes(from)).toBe(true)

  return { ...env, payload: Buffer.from(bytes.replace(from, to), 'utf8').toString('base64url') }
}

function tamperSig(env: OwnerGrantEnvelope): OwnerGrantEnvelope {
  const sig = Buffer.from(env.sig, 'base64url')
  sig[5] ^= 0x01

  return { ...env, sig: sig.toString('base64url') }
}

describe.skipIf(!HAVE_PYTHON)('E-10: a Node-signed v1 envelope verifies in the Python verifier', () => {
  test('E-10a signed by Electron, verified by hermes_owner_grant (pipeline + pure + cryptography); tampering refused', async () => {
    const base = fs.mkdtempSync(path.join(os.tmpdir(), 'ogx-'))
    tmpDirs.push(base)
    const grantsDir = path.join(base, 'grants')
    const store = createOwnerKeyStore({ safeStorage: mockSafeStorage, keyDir: path.join(base, 'key') })
    const info = store.ensure()
    store.setAnchor({ keys: [{ kid: info.kid, pub: info.pub, status: 'active' }] })
    const now = Date.now()
    const ownerUid = process.getuid!()

    const text = 'Enable the review budget gate — then “ship” 🚀 once CI is green.'

    const req: SignRequest = {
      text,
      gesture: 'proposal',
      sourceSession: { session_id: 'mgr-1', message_id: null, role: 'user' },
      targets: [
        { profile: 'default', session_id: 'w-1', claude_session_id: 'claude-1', backend: 'bk_a' },
        { profile: 'work', session_id: 'w-2', claude_session_id: null, backend: 'bk_b' }
      ],
      scope: ['conductor:gate:review-budget-enable', 'conductor:prod:target'],
      subject: { 'conductor:prod:target': 'sha256:deadbeef' }
    }

    const signed = await confirmAndSignGrants(req, {
      store,
      grantsDir,
      now: () => now,
      ownerUid,
      confirm: async () => ({ confirmed: true }),
      touchId: { canPrompt: () => false, prompt: async () => {} }
    })

    if (signed.cancelled) {
      throw new Error('expected grants')
    }

    const [a, b] = (signed as SignedOutcome).grants
    const textSha = createHash('sha256').update(text, 'utf8').digest('hex')

    const q1 = {
      session: 'w-1',
      claude_session: 'claude-1',
      now: now + 1000,
      text_sha: textSha,
      scopes: ['conductor:prod:target'],
      subject: 'sha256:deadbeef'
    }

    const result = runPython({
      repo: REPO,
      grants_dir: grantsDir,
      kid: info.kid,
      pub: info.pub,
      owner_forward: {
        envelope: a.envelope,
        tampered: tamperSig(a.envelope),
        text,
        backend: a.backend
      },
      cases: [
        { name: 'a', envelope: a.envelope, query: q1, lookup: true },
        { name: 'b', envelope: b.envelope, query: { session: 'w-2', now: now + 1000, text_sha: textSha, scopes: ['conductor:gate:review-budget-enable'] }, lookup: true },
        { name: 'wrong-session', envelope: b.envelope, query: { ...q1, claude_session: undefined } },
        { name: 'wrong-subject', envelope: a.envelope, query: { ...q1, subject: 'sha256:other' } },
        { name: 'tampered-text', envelope: tamperPayload(a.envelope, 'green', 'greeN'), query: q1 },
        { name: 'tampered-scope', envelope: tamperPayload(a.envelope, 'review-budget', 'review-budgeu'), query: q1 },
        { name: 'tampered-sig', envelope: tamperSig(a.envelope), query: q1 }
      ]
    })

    // Same kid derivation on both sides.
    expect(result.kid_py).toBe(info.kid)
    const byName = Object.fromEntries(result.cases.map((c: any) => [c.name, c]))

    for (const [name, grant] of [['a', a], ['b', b]] as const) {
      const row = byName[name]
      expect(row.grant_id_py, name).toBe(grant.grantId)
      expect(row.sign_bytes_prefix_ok, name).toBe(true)
      expect(row.pure_ok, name).toBe(true)
      expect(row.crypto_ok, name).toBe(true)
      // Python's signer-side encoder reproduces the exact bytes Node signed.
      expect(row.reencode_identical, name).toBe(true)
      expect(row.envelope, name).toMatchObject({ ok: true, reason: null, grant_id: grant.grantId })
      // The grant file Electron wrote is found by the Python lookup in the anchor's grants_dir.
      expect(row.lookup, name).toMatchObject({ ok: true, grant_id: grant.grantId })
      expect(path.basename(grant.path)).toBe(`${now}-${grant.grantId}.json`)
    }

    expect(byName['wrong-session'].envelope).toMatchObject({ ok: false, reason: 'session_mismatch' })
    expect(byName['wrong-subject'].envelope).toMatchObject({ ok: false, reason: 'subject_mismatch' })

    expect(result.owner_forward_verifier_configured).toBe(true)
    expect(result.owner_forward.good).toMatchObject({
      result: { results: [{ target_session_id: 'w-1', status: 'delivered', detail: null }] }
    })
    expect(result.owner_forward.tampered.error.code).toBe(4127)
    // The same envelope is refused once the anchor revokes the key: the anchor is the trust root.
    expect(result.owner_forward.revoked.error.code).toBe(4127)

    for (const name of ['tampered-text', 'tampered-scope', 'tampered-sig']) {
      const row = byName[name]
      expect(row.envelope, name).toMatchObject({ ok: false, reason: 'bad_signature' })
      expect(row.pure_ok, name).toBe(false)
      expect(row.crypto_ok, name).toBe(false)
    }
  }, 60_000)
})

test('E-10 prerequisite: the Python verifier is available (otherwise E-10 was skipped)', () => {
  if (!HAVE_PYTHON) {
    console.warn(`E-10 SKIPPED: no Python at ${PYTHON} or no hermes_owner_grant at ${REPO}`)
  }

  expect(true).toBe(true)
})
