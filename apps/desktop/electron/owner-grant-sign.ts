/**
 * Owner-grant v1 signing core (#60 U12, the non-UI half; VERIFY addendum §2.1-§2.3, §6; tests
 * E-7, E-8, E-10). The native dialog and IPC wiring are NOT here: `confirm` and Touch ID are
 * injected ports, and nothing in this module is reachable from the renderer.
 *
 * `confirmAndSignGrants(request, ports)`:
 *  1. validates the request (targets ≤ 5, scope grammar, prod subject, self-target rule, text)
 *     and refuses BEFORE any dialog when it can't be signed (incl. the anchor gate);
 *  2. builds the confirm model the dialog renders: every target, every scope on its own line
 *     with its class, label (or "unrecognized") and the grant's expiry, and the text;
 *  3. awaits the owner's confirm; Cancel, a missing "unrecognized scope" acknowledgement, or a
 *     failed Touch ID (required for `conductor:prod:*` when available) sign and write NOTHING;
 *  4. signs ONE grant per backend, all sharing one `decision_id`, and writes each grant file
 *     `<issued_at>-<grant_id>.json` (0600, O_EXCL/no-clobber) before returning any envelope.
 *
 * Byte compatibility with `hermes_owner_grant` (proved by E-10): the envelope is
 * `{format,kid,payload,sig}`, `sig = Ed25519(b"hermes-owner-grant/v1\0" + payload)`, the payload is
 * compact JSON with sorted keys (identical to `envelope.encode_payload`), `grant_id = "og_" +
 * base32(sha256(payload))[:26].lower()`, and `text_len` is the UTF-8 byte length.
 *
 * One signed extension beyond addendum §2.2: `forward_targets`, the sorted `<profile>:<session_id>`
 * list the gateway's `owner.forward` binds delivery to. `targets[].session_id` stays the bare Hermes
 * session id so hooks can match it against `HERMES_SESSION_ID`; the verifier's payload schema
 * allows extra signed fields.
 */

import { createHash, randomBytes as nodeRandomBytes } from 'node:crypto'
import nodeFs from 'node:fs'
import path from 'node:path'

import { GRANT_FORMAT, MAX_PAYLOAD_BYTES, type OwnerGrantEnvelope, type OwnerKeyStore } from './owner-grant-key'

export type { OwnerGrantEnvelope } from './owner-grant-key'

export const GRANT_AUDIENCE: string[] = ['hermes-owner-forward', 'hermes-owner-verify']
export const GRANT_VERSION = 1
export const DELIVER_WINDOW_MS = 60_000
export const MAX_TARGETS = 5
export const MAX_TEXT_BYTES = 24 * 1024
export const GESTURES = ['composer_signed', 'menu', 'proposal', 'selection', 'slash_to'] as const
export const SOURCE_ROLES = ['assistant', 'peer', 'user'] as const

export type Gesture = (typeof GESTURES)[number]
export type ScopeClass = 'allowlist' | 'gate' | 'marker' | 'prod' | 'quote-only'

export interface ScopeClassPolicy {
  default_ttl_ms: number
  max_ttl_ms: number
  single_use: boolean
  subject_required: boolean
  touch_id: 'never' | 'when_available'
}

/** Mirrors `hermes_owner_grant/scopes.json` `classes` (a parity test pins them equal). */
export const SCOPE_CLASS_POLICY: Record<ScopeClass, ScopeClassPolicy> = {
  allowlist: { default_ttl_ms: 43_200_000, max_ttl_ms: 259_200_000, single_use: false, subject_required: false, touch_id: 'never' },
  gate: { default_ttl_ms: 43_200_000, max_ttl_ms: 259_200_000, single_use: false, subject_required: false, touch_id: 'never' },
  marker: { default_ttl_ms: 3_600_000, max_ttl_ms: 14_400_000, single_use: true, subject_required: false, touch_id: 'never' },
  prod: { default_ttl_ms: 900_000, max_ttl_ms: 3_600_000, single_use: true, subject_required: true, touch_id: 'when_available' },
  'quote-only': { default_ttl_ms: 604_800_000, max_ttl_ms: 604_800_000, single_use: false, subject_required: false, touch_id: 'never' }
}

/** Mirrors `hermes_owner_grant/scopes.json` `scopes` (dialog labels only; parity-tested). */
export const SCOPE_LABELS: Record<string, string> = {
  'conductor:allowlist:member-profile': 'Allowlisted member profile',
  'conductor:gate:job-store-write-block': 'Block job store writes',
  'conductor:gate:lane-test-budget-enable': 'Enable lane test budget',
  'conductor:gate:pr-discipline-enable': 'Enable PR discipline',
  'conductor:gate:review-budget-enable': 'Enable review budget',
  'conductor:marker:bypass': 'Bypass marker',
  'conductor:marker:restore': 'Restore marker',
  'conductor:prod:target': 'Production target'
}

const SCOPE_RE = /^conductor:(allowlist|gate|marker|prod):([a-z0-9][a-z0-9-]{0,62})$/
const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

export type OwnerGrantSignErrorCode =
  | 'bad_gesture'
  | 'bad_scope'
  | 'bad_source'
  | 'bad_subject'
  | 'bad_target'
  | 'bad_text'
  | 'bad_ttl'
  | 'duplicate_target'
  | 'empty_text'
  | 'grant_exists'
  | 'no_targets'
  | 'payload_too_large'
  | 'self_target'
  | 'subject_required'
  | 'target_not_bound'
  | 'text_too_long'
  | 'too_many_targets'

export class OwnerGrantSignError extends Error {
  readonly code: OwnerGrantSignErrorCode

  constructor(code: OwnerGrantSignErrorCode, message: string) {
    super(`${code}: ${message}`)
    this.name = 'OwnerGrantSignError'
    this.code = code
  }
}

export interface ParsedScope {
  value: string
  scopeClass: Exclude<ScopeClass, 'quote-only'>
  name: string
  catalogued: boolean
  label: string | null
  singleUse: boolean
  subjectRequired: boolean
  touchId: 'never' | 'when_available'
}

export function parseScope(value: string): ParsedScope {
  const match = typeof value === 'string' ? SCOPE_RE.exec(value) : null

  if (!match) {
    throw new OwnerGrantSignError('bad_scope', `scope ${JSON.stringify(value)} is outside the owner-grant grammar`)
  }

  const scopeClass = match[1] as ParsedScope['scopeClass']
  const policy = SCOPE_CLASS_POLICY[scopeClass]
  const label = Object.prototype.hasOwnProperty.call(SCOPE_LABELS, value) ? SCOPE_LABELS[value] : null

  return {
    value,
    scopeClass,
    name: match[2],
    catalogued: label !== null,
    label,
    singleUse: policy.single_use,
    subjectRequired: policy.subject_required,
    touchId: policy.touch_id
  }
}

// -- bytes -------------------------------------------------------------------------------------

function isWellFormed(text: string): boolean {
  return !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(text)
}

function canonical(value: unknown, where: string): unknown {
  if (value === null || typeof value === 'boolean') {
    return value
  }

  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) {
      throw new TypeError(`${where}: only safe integers are signed`)
    }

    return value
  }

  if (typeof value === 'string') {
    if (!isWellFormed(value)) {
      throw new TypeError(`${where}: lone UTF-16 surrogate`)
    }

    return value
  }

  if (Array.isArray(value)) {
    return value.map((v, i) => canonical(v, `${where}[${i}]`))
  }

  if (typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    const out: Record<string, unknown> = {}

    // Keys are ASCII here, so UTF-16 order equals Python's code point order.
    for (const key of Object.keys(value as object).sort()) {
      out[key] = canonical((value as Record<string, unknown>)[key], `${where}.${key}`)
    }

    return out
  }

  throw new TypeError(`${where}: unsupported JSON value`)
}

/** Compact JSON, keys sorted recursively, UTF-8: byte-identical to envelope.encode_payload. */
export function encodePayload(obj: Record<string, unknown>): Buffer {
  return Buffer.from(JSON.stringify(canonical(obj, 'payload')), 'utf8')
}

const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

function base32(bytes: Uint8Array): string {
  let bits = 0
  let acc = 0
  let out = ''

  for (const byte of bytes) {
    acc = (acc << 8) | byte
    bits += 8

    while (bits >= 5) {
      out += B32[(acc >>> (bits - 5)) & 31]
      bits -= 5
    }
  }

  if (bits > 0) {
    out += B32[(acc << (5 - bits)) & 31]
  }

  return out
}

export function deriveGrantId(payload: Uint8Array): string {
  return 'og_' + base32(createHash('sha256').update(payload).digest()).slice(0, 26).toLowerCase()
}

export function grantFileName(issuedAt: number, grantId: string): string {
  if (!Number.isSafeInteger(issuedAt) || issuedAt < 0 || !/^og_[a-z2-7]{26}$/.test(grantId)) {
    throw new TypeError('grant file name needs a non-negative int issued_at and an og_ id')
  }

  return `${issuedAt}-${grantId}.json`
}

/** Write one grant file: temp (O_EXCL, 0600) → hard link to the final name (never replaces an
 *  existing grant) → drop the temp name. The grants dir is created 0700 when missing. */
export function writeGrantFile(
  grantsDir: string,
  envelope: OwnerGrantEnvelope | Record<string, string>,
  issuedAt: number,
  grantId: string,
  fs: typeof nodeFs = nodeFs
): string {
  const name = grantFileName(issuedAt, grantId)
  fs.mkdirSync(grantsDir, { recursive: true, mode: 0o700 })
  const st = fs.lstatSync(grantsDir)

  if (!st.isDirectory()) {
    throw new OwnerGrantSignError('bad_target', 'the grants path is not a directory')
  }

  const text = JSON.stringify(canonical({ ...envelope }, 'envelope'))
  const finalPath = path.join(grantsDir, name)
  const tmp = path.join(grantsDir, `.${name}.${process.pid}.${nodeRandomBytes(6).toString('hex')}.tmp`)
  const fd = fs.openSync(tmp, 'wx', 0o600)

  try {
    fs.writeSync(fd, text)
    fs.fsyncSync(fd)
  } finally {
    fs.closeSync(fd)
  }

  try {
    fs.linkSync(tmp, finalPath)
  } catch (error: any) {
    if (error?.code === 'EEXIST') {
      throw new OwnerGrantSignError('grant_exists', `${name} already exists`)
    }

    throw error
  } finally {
    fs.unlinkSync(tmp)
  }

  return finalPath
}

// -- request, plan, confirm model ------------------------------------------------------------

export interface SignTarget {
  profile: string
  session_id: string
  claude_session_id: string | null
  /** The backend spawn id this target is delivered through (one grant per backend). */
  backend: string
  /** Title main resolved itself (never a renderer label). Display only; never signed. */
  title?: string
}

export interface SignRequest {
  text: string
  gesture: Gesture
  sourceSession: { session_id: string; message_id: string | null; role: 'user' | 'assistant' | 'peer' | null }
  targets: SignTarget[]
  scope: string[]
  subject?: Record<string, string>
  ttlMs?: number
}

export interface ConfirmScopeLine {
  scope: string
  scopeClass: ParsedScope['scopeClass']
  label: string | null
  catalogued: boolean
  singleUse: boolean
  subject: string | null
  expiresAt: number
}

export interface ConfirmModel {
  gesture: Gesture
  sourceSession: SignRequest['sourceSession']
  targets: Array<{ profile: string; session_id: string; claude_session_id: string | null; backend: string; title: string | null }>
  scopes: ConfirmScopeLine[]
  quoteOnly: boolean
  ttlMs: number
  /** Estimated from the time the dialog opens; the signed value is set at confirm time. */
  expiresAt: number
  deliverWindowMs: number
  grants: number
  text: string
  textChars: number
  textBytes: number
  requiresTouchId: boolean
  requiresUnrecognizedAck: boolean
}

export interface SignPorts {
  store: Pick<OwnerKeyStore, 'assertMaySign' | 'signEnvelope'>
  grantsDir: string
  now: () => number
  ownerUid: number
  confirm: (model: ConfirmModel) => Promise<{ confirmed: boolean; acknowledgedUnrecognized?: boolean }>
  touchId?: { canPrompt(): boolean; prompt(reason: string): Promise<void> }
  randomBytes?: (n: number) => Buffer
  fs?: typeof nodeFs
}

export interface SignedGrant {
  backend: string
  grantId: string
  envelope: OwnerGrantEnvelope
  path: string
}

export type CancelledOutcome = { cancelled: true; reason: 'dialog' | 'touch_id' | 'unrecognized_scope' }
export type SignedOutcome = { cancelled: false; decisionId: string; grants: SignedGrant[] }
export type SignOutcome = CancelledOutcome | SignedOutcome

interface Plan {
  text: string
  gesture: Gesture
  source: SignRequest['sourceSession']
  targets: SignTarget[]
  scopes: ParsedScope[]
  subject: Record<string, string>
  ttlMs: number
}

function fail(code: OwnerGrantSignErrorCode, message: string): never {
  throw new OwnerGrantSignError(code, message)
}

function nonEmpty(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && !value.includes('\u0000')
}

function plan(req: SignRequest): Plan {
  const text = req?.text

  if (typeof text !== 'string') {
    fail('bad_text', 'text must be a string')
  }

  if (!text.trim()) {
    fail('empty_text', 'the text is empty')
  }

  if (!isWellFormed(text) || text.includes('\u0000')) {
    fail('bad_text', 'the text is not well-formed')
  }

  if (text.trimStart().startsWith('/')) {
    fail('bad_text', "a signed decision can't start with '/'")
  }

  if (Buffer.byteLength(text, 'utf8') > MAX_TEXT_BYTES) {
    fail('text_too_long', `the text exceeds ${MAX_TEXT_BYTES} UTF-8 bytes`)
  }

  if (!(GESTURES as readonly string[]).includes(req.gesture)) {
    fail('bad_gesture', `gesture must be one of ${GESTURES.join(', ')}`)
  }

  const source = req.sourceSession

  if (
    !source ||
    !nonEmpty(source.session_id) ||
    !(source.message_id === null || nonEmpty(source.message_id)) ||
    !(source.role === null || (SOURCE_ROLES as readonly string[]).includes(source.role))
  ) {
    fail('bad_source', 'source_session needs a session_id, a message_id or null, and a role or null')
  }

  const targets = Array.isArray(req.targets) ? req.targets : []

  if (targets.length === 0) {
    fail('no_targets', 'a grant needs at least one target')
  }

  if (targets.length > MAX_TARGETS) {
    fail('too_many_targets', `at most ${MAX_TARGETS} targets per decision`)
  }

  const seen = new Set<string>()

  for (const t of targets) {
    if (
      !t ||
      !PROFILE_RE.test(String(t.profile)) ||
      !nonEmpty(t.session_id) ||
      !(t.claude_session_id === null || nonEmpty(t.claude_session_id)) ||
      !nonEmpty(t.backend)
    ) {
      fail('bad_target', 'each target needs a profile, a session_id, a claude_session_id or null, and a backend')
    }

    if (seen.has(t.session_id)) {
      fail('duplicate_target', `session ${t.session_id} is targeted twice`)
    }

    seen.add(t.session_id)
  }

  if (req.gesture === 'composer_signed') {
    if (targets.length !== 1 || targets[0].session_id !== source.session_id) {
      fail('self_target', 'a composer-signed decision targets only its own session')
    }
  } else if (seen.has(source.session_id)) {
    fail('self_target', 'a forward cannot target its own source session')
  }

  const scopeValues = Array.isArray(req.scope) ? req.scope : fail('bad_scope', 'scope must be a list')
  const scopes = [...new Set(scopeValues)].sort().map(parseScope)
  const subjectIn = req.subject ?? {}

  if (typeof subjectIn !== 'object' || Array.isArray(subjectIn)) {
    fail('bad_subject', 'subject must map scopes to strings')
  }

  const subject: Record<string, string> = {}

  for (const [scope, value] of Object.entries(subjectIn)) {
    if (!scopes.some(s => s.value === scope)) {
      fail('bad_subject', `subject names ${scope}, which the grant doesn't carry`)
    }

    if (!nonEmpty(value)) {
      fail('bad_subject', `subject for ${scope} must be a non-empty string`)
    }

    subject[scope] = value
  }

  for (const s of scopes) {
    if (s.subjectRequired && !(s.value in subject)) {
      fail('subject_required', `${s.value} needs a subject (the exact action digest)`)
    }
  }

  // T-6: a hook trusts its HERMES_SESSION_ID only as far as settings `env` can't move it, which is
  // not far; the Claude session id from hook stdin is what binds. A grant carrying a conductor scope
  // is therefore bound to each target's LIVE CLI or not signed at all. Quote-only may carry null.
  const unbound = scopes.length ? targets.find(t => t.claude_session_id === null) : undefined

  if (unbound) {
    fail('target_not_bound', `session ${unbound.session_id} has no running Claude CLI to bind a scope to`)
  }

  const classes = scopes.length ? scopes.map(s => SCOPE_CLASS_POLICY[s.scopeClass]) : [SCOPE_CLASS_POLICY['quote-only']]
  const cap = Math.min(...classes.map(p => p.max_ttl_ms))
  let ttlMs = Math.min(...classes.map(p => p.default_ttl_ms))

  if (req.ttlMs !== undefined) {
    if (!Number.isSafeInteger(req.ttlMs) || req.ttlMs <= 0) {
      fail('bad_ttl', 'ttlMs must be a positive integer')
    }

    ttlMs = req.ttlMs
  }

  return { text, gesture: req.gesture, source: { ...source }, targets, scopes, subject, ttlMs: Math.min(ttlMs, cap) }
}

function sortTargets<T extends { profile: string; session_id: string }>(targets: T[]): T[] {
  return [...targets].sort((a, b) =>
    a.session_id === b.session_id ? (a.profile < b.profile ? -1 : 1) : a.session_id < b.session_id ? -1 : 1
  )
}

function confirmModel(p: Plan, now: number, requiresTouchId: boolean): ConfirmModel {
  const expiresAt = now + p.ttlMs

  return {
    gesture: p.gesture,
    sourceSession: { ...p.source },
    targets: sortTargets(p.targets).map(t => ({
      profile: t.profile,
      session_id: t.session_id,
      claude_session_id: t.claude_session_id,
      backend: t.backend,
      title: typeof t.title === 'string' && t.title ? t.title : null
    })),
    scopes: p.scopes.map(s => ({
      scope: s.value,
      scopeClass: s.scopeClass,
      label: s.label,
      catalogued: s.catalogued,
      singleUse: s.singleUse,
      subject: p.subject[s.value] ?? null,
      expiresAt
    })),
    quoteOnly: p.scopes.length === 0,
    ttlMs: p.ttlMs,
    expiresAt,
    deliverWindowMs: DELIVER_WINDOW_MS,
    grants: new Set(p.targets.map(t => t.backend)).size,
    text: p.text,
    textChars: p.text.length,
    textBytes: Buffer.byteLength(p.text, 'utf8'),
    requiresTouchId,
    requiresUnrecognizedAck: p.scopes.some(s => !s.catalogued)
  }
}

function lowerBase32Id(prefix: string, bytes: Buffer): string {
  return prefix + base32(bytes).toLowerCase()
}

/** Validate, confirm (injected dialog + Touch ID), sign one grant per backend, write the files. */
export async function confirmAndSignGrants(req: SignRequest, ports: SignPorts): Promise<SignOutcome> {
  const p = plan(req)
  const scopeValues = p.scopes.map(s => s.value)
  // The anchor gate runs before the dialog: never ask the owner to confirm what can't be signed.
  ports.store.assertMaySign(scopeValues)

  const touchCapable = (() => {
    try {
      return Boolean(ports.touchId?.canPrompt())
    } catch {
      return false
    }
  })()

  const requiresTouchId = touchCapable && p.scopes.some(s => s.touchId === 'when_available')
  const model = confirmModel(p, ports.now(), requiresTouchId)
  const answer = await ports.confirm(model)

  if (!answer || answer.confirmed !== true) {
    return { cancelled: true, reason: 'dialog' }
  }

  if (model.requiresUnrecognizedAck && answer.acknowledgedUnrecognized !== true) {
    return { cancelled: true, reason: 'unrecognized_scope' }
  }

  if (requiresTouchId) {
    try {
      await ports.touchId!.prompt('sign a production decision as you')
    } catch {
      return { cancelled: true, reason: 'touch_id' }
    }
  }

  const random = ports.randomBytes ?? nodeRandomBytes
  const issuedAt = ports.now()
  const decisionId = lowerBase32Id('od_', random(16))
  const byBackend = new Map<string, SignTarget[]>()

  for (const t of p.targets) {
    byBackend.set(t.backend, [...(byBackend.get(t.backend) ?? []), t])
  }

  const scope = scopeValues
  const singleUse = p.scopes.filter(s => s.singleUse).map(s => s.value)
  const textSha = createHash('sha256').update(p.text, 'utf8').digest('hex')
  const textLen = Buffer.byteLength(p.text, 'utf8')
  const signed: Array<{ backend: string; grantId: string; envelope: OwnerGrantEnvelope; bytes: Buffer }> = []

  for (const backend of [...byBackend.keys()].sort()) {
    const targets = sortTargets(byBackend.get(backend)!)

    const payload = {
      v: GRANT_VERSION,
      aud: [...GRANT_AUDIENCE],
      decision_id: decisionId,
      issued_at: issuedAt,
      deliver_by: issuedAt + DELIVER_WINDOW_MS,
      expires_at: issuedAt + p.ttlMs,
      owner_uid: ports.ownerUid,
      backend,
      nonce: random(16).toString('base64url'),
      gesture: p.gesture,
      confirm: requiresTouchId ? 'touch_id' : 'native_dialog',
      source_session: { session_id: p.source.session_id, message_id: p.source.message_id, role: p.source.role },
      targets: targets.map(t => ({ session_id: t.session_id, claude_session_id: t.claude_session_id })),
      forward_targets: targets.map(t => `${t.profile}:${t.session_id}`).sort(),
      scope,
      single_use: singleUse,
      subject: { ...p.subject },
      text: p.text,
      text_sha256: textSha,
      text_len: textLen
    }

    const bytes = encodePayload(payload)

    if (bytes.length > MAX_PAYLOAD_BYTES) {
      fail('payload_too_large', `the signed payload exceeds ${MAX_PAYLOAD_BYTES} bytes`)
    }

    signed.push({ backend, grantId: deriveGrantId(bytes), envelope: ports.store.signEnvelope(bytes, scope), bytes })
  }

  // Files first (the audit trail exists before any envelope leaves this module).
  const grants = signed.map(g => ({
    backend: g.backend,
    grantId: g.grantId,
    envelope: g.envelope,
    path: writeGrantFile(ports.grantsDir, g.envelope, issuedAt, g.grantId, ports.fs)
  }))

  return { cancelled: false, decisionId, grants }
}

export { GRANT_FORMAT }
