/**
 * b10 §3, §4: the session binding store. Electron main ONLY.
 *
 * Durable owner-signed session binding records (`hermes-session-binding/v1`), mapping a Hermes session
 * to its bound project workspace:
 * - format `hermes-session-binding/v1`, domain prefix `hermes-session-binding/v1\0`;
 * - audience `hermes-main` only;
 * - path `<grants_dir>/session-bindings/<profile>/<hermes_session_id>.json` (dir 0700, file 0600);
 * - verify-on-load accepts ONLY active-kid records (retired or unknown kid -> `needs_reconfirm`);
 * - in-memory high-water mark per (profile, hermes_session_id) to prevent replay/rollback;
 * - `loadAll()` is re-runnable: a reload re-verifies the records this run already holds (same seq
 *   and nonce) and never accepts one below the in-run high-water. Main calls it only after the
 *   owner key and anchor are loaded, so a verified record never loads as `needs_reconfirm`;
 * - `resignAll()` re-signs in-memory verified records with the current active key upon key rotation.
 */

import { createPublicKey, randomBytes as nodeRandomBytes, verify as edVerify } from 'node:crypto'
import nodeFs from 'node:fs'
import path from 'node:path'

import {
  BINDING_AUDIENCE,
  BINDING_DOMAIN_PREFIX,
  BINDING_FORMAT,
  b64urlDecode,
  defaultOwnerGrantsDir,
  MAX_PAYLOAD_BYTES,
  OwnerKeyError,
  type OwnerDomainEnvelope,
  type OwnerKeyStore
} from './owner-grant-key'
import { readTrustedOwnerAnchor } from './owner-grant-anchor'
import { encodePayload } from './owner-grant-sign'

export type SessionBindingState = 'bound' | 'unbound'
export type SessionBindingRecordState = 'bound' | 'unbound' | 'needs_reconfirm'

export interface SessionBindingPayload {
  v: 1
  aud: readonly string[]
  owner_uid: number
  profile: string
  hermes_session_id: string
  state: SessionBindingState
  seq: number
  binding_nonce: string
  bound_at: number
  project_root: string | null
  repo_common_root: string | null
  repo_remote: string | null
  project_id: string | null
  carried_from: string | null
}

export interface SessionBindingRecord {
  ok: true
  state: SessionBindingRecordState
  profile: string
  hermes_session_id: string
  seq: number
  binding_nonce: string
  bound_at: number
  project_root: string | null
  repo_common_root: string | null
  repo_remote: string | null
  project_id: string | null
  carried_from: string | null
  envelope: OwnerDomainEnvelope
  payload: SessionBindingPayload
  verified: boolean
  record?: SessionBindingRecord
}

export type SessionBindingRefusal =
  | 'signing_off'
  | 'bad_input'
  | 'seq_low'
  | 'symlink_refused'
  | 'bad_target'
  | 'io_error'
  | 'bad_signature'
  | 'malformed'

export interface SessionBindingRefusalResult {
  ok: false
  reason: SessionBindingRefusal
  error?: unknown
}

export type SessionBindingOutcome = SessionBindingRecord | SessionBindingRefusalResult

export interface BindParams {
  profile: string
  hermes_session_id: string
  project_root: string
  repo_common_root: string | null
  repo_remote?: string | null
  project_id?: string | null
  carried_from?: string | null
  seq?: number
}

export interface RefusedBindingFile {
  path: string
  reason: string
  error?: unknown
}

export interface LoadAllRecords extends Array<SessionBindingRecord> {
  refused: RefusedBindingFile[]
  records: SessionBindingRecord[]
  loaded: SessionBindingRecord[]
}

export interface ResignAllRecords extends Array<SessionBindingRecord> {
  resigned: number
  records: SessionBindingRecord[]
}

export interface AnchorKeyView {
  kid: string
  pub: string
  status: string
  retiredAt?: number | null
}

export interface AnchorLike {
  ok: boolean
  keys: ReadonlyArray<AnchorKeyView>
}

export interface SessionBindingStorePorts {
  store: Pick<OwnerKeyStore, 'assertMaySign' | 'signDomain' | 'publicInfo' | 'anchorState'>
  grantsDir?: string
  ownerUid?: number
  now?: () => number
  randomBytes?: (n: number) => Buffer
  fs?: typeof nodeFs
  readAnchor?: () => AnchorLike | null
}

const B32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

export function generateBindingNonce(random: (n: number) => Buffer = nodeRandomBytes): string {
  const bytes = random(16)
  let bits = 0
  let acc = 0
  let out = ''

  for (const byte of bytes) {
    acc = (acc << 8) | byte
    bits += 8

    while (bits >= 5) {
      out += B32_ALPHABET[(acc >>> (bits - 5)) & 31]
      bits -= 5
    }
  }

  if (bits > 0) {
    out += B32_ALPHABET[(acc << (5 - bits)) & 31]
  }

  return out
}

export function isValidPathComponent(value: unknown): value is string {
  if (typeof value !== 'string' || value.length === 0) {
    return false
  }

  if (
    value.includes('/') ||
    value.includes('\\') ||
    value.includes('..') ||
    value.includes('\0') ||
    value.includes('\u0000')
  ) {
    return false
  }

  return true
}

function isValidProjectRoot(root: unknown): root is string {
  if (typeof root !== 'string' || root.length === 0) {
    return false
  }

  if (!root.startsWith('/')) {
    return false
  }

  if (path.posix.normalize(root) !== root) {
    return false
  }

  if (root !== '/' && root.endsWith('/')) {
    return false
  }

  return true
}

function isValidRepoCommonRoot(root: unknown): boolean {
  if (root === null || root === undefined) {
    return true
  }

  if (typeof root !== 'string' || root.length === 0) {
    return false
  }

  if (!root.startsWith('/')) {
    return false
  }

  if (path.posix.normalize(root) !== root) {
    return false
  }

  if (root !== '/' && root.endsWith('/')) {
    return false
  }

  return true
}

function writeBindingFile(
  grantsDir: string,
  profile: string,
  hermesSessionId: string,
  envelope: OwnerDomainEnvelope,
  fs: typeof nodeFs = nodeFs,
  random: (n: number) => Buffer = nodeRandomBytes
): { ok: true; path: string } | { ok: false; reason: SessionBindingRefusal } {
  if (!isValidPathComponent(profile) || !isValidPathComponent(hermesSessionId)) {
    return { ok: false, reason: 'bad_input' }
  }

  try {
    fs.mkdirSync(grantsDir, { recursive: true, mode: 0o700 })
  } catch (err: any) {
    if (err?.code !== 'EEXIST') {
      return { ok: false, reason: 'io_error' }
    }
  }

  let realGrantsDir: string

  try {
    realGrantsDir = fs.realpathSync(grantsDir)
  } catch {
    return { ok: false, reason: 'bad_target' }
  }

  try {
    const grantsSt = fs.lstatSync(realGrantsDir)

    if (!grantsSt.isDirectory() || grantsSt.isSymbolicLink()) {
      return { ok: false, reason: grantsSt.isSymbolicLink() ? 'symlink_refused' : 'bad_target' }
    }
  } catch {
    return { ok: false, reason: 'bad_target' }
  }

  const bindingsDir = path.join(realGrantsDir, 'session-bindings')

  try {
    const st = fs.lstatSync(bindingsDir)

    if (st.isSymbolicLink()) {
      return { ok: false, reason: 'symlink_refused' }
    }

    if (!st.isDirectory()) {
      return { ok: false, reason: 'bad_target' }
    }
  } catch (err: any) {
    if (err?.code === 'ENOENT') {
      fs.mkdirSync(bindingsDir, { recursive: true, mode: 0o700 })
      const st = fs.lstatSync(bindingsDir)

      if (st.isSymbolicLink()) {
        return { ok: false, reason: 'symlink_refused' }
      }

      if (!st.isDirectory()) {
        return { ok: false, reason: 'bad_target' }
      }
    } else {
      return { ok: false, reason: 'io_error' }
    }
  }

  const profileDir = path.join(bindingsDir, profile)

  try {
    const st = fs.lstatSync(profileDir)

    if (st.isSymbolicLink()) {
      return { ok: false, reason: 'symlink_refused' }
    }

    if (!st.isDirectory()) {
      return { ok: false, reason: 'bad_target' }
    }
  } catch (err: any) {
    if (err?.code === 'ENOENT') {
      fs.mkdirSync(profileDir, { recursive: true, mode: 0o700 })
      const st = fs.lstatSync(profileDir)

      if (st.isSymbolicLink()) {
        return { ok: false, reason: 'symlink_refused' }
      }

      if (!st.isDirectory()) {
        return { ok: false, reason: 'bad_target' }
      }
    } else {
      return { ok: false, reason: 'io_error' }
    }
  }

  const finalPath = path.join(profileDir, `${hermesSessionId}.json`)

  try {
    const st = fs.lstatSync(finalPath)

    if (st.isSymbolicLink()) {
      return { ok: false, reason: 'symlink_refused' }
    }
  } catch (err: any) {
    if (err?.code !== 'ENOENT') {
      return { ok: false, reason: 'io_error' }
    }
  }

  const text = JSON.stringify({
    format: envelope.format,
    kid: envelope.kid,
    payload: envelope.payload,
    sig: envelope.sig
  })

  const tmpName = `.${hermesSessionId}.${process.pid}.${random(6).toString('hex')}.tmp`
  const tmpPath = path.join(profileDir, tmpName)

  let fd: number

  try {
    fd = fs.openSync(tmpPath, 'wx', 0o600)
  } catch {
    return { ok: false, reason: 'io_error' }
  }

  try {
    fs.writeSync(fd, text)
    fs.fsyncSync(fd)
  } finally {
    fs.closeSync(fd)
  }

  try {
    fs.renameSync(tmpPath, finalPath)
  } catch {
    try {
      fs.unlinkSync(tmpPath)
    } catch {
      // ignore unlink failure
    }

    return { ok: false, reason: 'io_error' }
  }

  return { ok: true, path: finalPath }
}

type VerifyResult =
  | { state: 'verified'; payload: SessionBindingPayload; envelope: OwnerDomainEnvelope }
  | { state: 'needs_reconfirm'; payload: SessionBindingPayload; envelope: OwnerDomainEnvelope }
  | { state: 'refused'; reason: SessionBindingRefusal }

function verifyBindingEnvelope(
  envelope: unknown,
  expectedProfile: string,
  expectedSessionId: string,
  ports: SessionBindingStorePorts
): VerifyResult {
  const env = envelope as Record<string, unknown> | null

  if (!env || typeof env !== 'object' || env.format !== BINDING_FORMAT || typeof env.kid !== 'string') {
    return { state: 'refused', reason: 'malformed' }
  }

  if (typeof env.payload !== 'string' || typeof env.sig !== 'string') {
    return { state: 'refused', reason: 'malformed' }
  }

  const payloadBytes = b64urlDecode(env.payload)
  const sigBytes = b64urlDecode(env.sig)

  if (
    !payloadBytes ||
    !sigBytes ||
    payloadBytes.length === 0 ||
    payloadBytes.length > MAX_PAYLOAD_BYTES ||
    sigBytes.length !== 64
  ) {
    return { state: 'refused', reason: 'malformed' }
  }

  let claims: any

  try {
    claims = JSON.parse(payloadBytes.toString('utf8'))
  } catch {
    return { state: 'refused', reason: 'malformed' }
  }

  if (!claims || typeof claims !== 'object' || Array.isArray(claims)) {
    return { state: 'refused', reason: 'malformed' }
  }

  if (
    claims.v !== 1 ||
    !Array.isArray(claims.aud) ||
    claims.aud.length !== 1 ||
    claims.aud[0] !== 'hermes-main' ||
    claims.profile !== expectedProfile ||
    claims.hermes_session_id !== expectedSessionId ||
    (claims.state !== 'bound' && claims.state !== 'unbound') ||
    typeof claims.seq !== 'number' ||
    !Number.isSafeInteger(claims.seq) ||
    claims.seq <= 0 ||
    typeof claims.binding_nonce !== 'string' ||
    claims.binding_nonce.length === 0 ||
    typeof claims.bound_at !== 'number'
  ) {
    return { state: 'refused', reason: 'malformed' }
  }

  let activePub: string | null = null

  if (ports.readAnchor) {
    const anchor = ports.readAnchor()

    if (anchor && anchor.ok) {
      const k = anchor.keys.find(x => x.kid === env.kid)

      if (!k) {
        return { state: 'needs_reconfirm', payload: claims, envelope: env as unknown as OwnerDomainEnvelope }
      }

      if (k.status === 'revoked') {
        return { state: 'refused', reason: 'bad_signature' }
      }

      if (k.status !== 'active') {
        return { state: 'needs_reconfirm', payload: claims, envelope: env as unknown as OwnerDomainEnvelope }
      }

      activePub = k.pub
    } else {
      return { state: 'needs_reconfirm', payload: claims, envelope: env as unknown as OwnerDomainEnvelope }
    }
  } else {
    const activeInfo = ports.store.publicInfo?.()
    const state = ports.store.anchorState?.()

    if (!state || state !== 'match') {
      return { state: 'needs_reconfirm', payload: claims, envelope: env as unknown as OwnerDomainEnvelope }
    }

    if (activeInfo && activeInfo.kid === env.kid) {
      activePub = activeInfo.pub
    } else {
      return { state: 'needs_reconfirm', payload: claims, envelope: env as unknown as OwnerDomainEnvelope }
    }
  }

  let ok = false

  try {
    const publicKey = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: activePub }, format: 'jwk' })
    ok = edVerify(null, Buffer.concat([BINDING_DOMAIN_PREFIX, payloadBytes]), publicKey, sigBytes)
  } catch {
    ok = false
  }

  if (!ok) {
    return { state: 'refused', reason: 'bad_signature' }
  }

  return { state: 'verified', payload: claims, envelope: env as unknown as OwnerDomainEnvelope }
}

export interface SessionBindingStore {
  bind(params: BindParams): SessionBindingOutcome
  unbind(profile: string, hermes_session_id: string): SessionBindingOutcome
  get(profile: string, hermes_session_id: string): SessionBindingRecord | null
  loadAll(): LoadAllRecords
  resignAll(): ResignAllRecords
}

export function createSessionBindingStore(ports: SessionBindingStorePorts): SessionBindingStore {
  const highWaterMarks = new Map<string, number>()
  const records = new Map<string, SessionBindingRecord>()

  function bind(params: BindParams): SessionBindingOutcome {
    if (!isValidPathComponent(params?.profile) || !isValidPathComponent(params?.hermes_session_id)) {
      return { ok: false, reason: 'bad_input' }
    }

    if (!isValidProjectRoot(params?.project_root)) {
      return { ok: false, reason: 'bad_input' }
    }

    if (!isValidRepoCommonRoot(params?.repo_common_root)) {
      return { ok: false, reason: 'bad_input' }
    }

    const key = `${params.profile}\0${params.hermes_session_id}`
    const currentHw = highWaterMarks.get(key) ?? 0
    const seq = params.seq !== undefined ? params.seq : currentHw + 1

    if (seq <= currentHw) {
      return { ok: false, reason: 'seq_low' }
    }

    try {
      ports.store.assertMaySign?.([])
    } catch {
      return { ok: false, reason: 'signing_off' }
    }

    const nonce = generateBindingNonce(ports.randomBytes)
    const bound_at = (ports.now ?? Date.now)()
    const owner_uid = ports.ownerUid ?? (typeof process.getuid === 'function' ? process.getuid() : 501)

    const payload: SessionBindingPayload = {
      v: 1,
      aud: [...BINDING_AUDIENCE],
      owner_uid,
      profile: params.profile,
      hermes_session_id: params.hermes_session_id,
      state: 'bound',
      seq,
      binding_nonce: nonce,
      bound_at,
      project_root: params.project_root,
      repo_common_root: params.repo_common_root ?? null,
      repo_remote: params.repo_remote ?? null,
      project_id: params.project_id ?? null,
      carried_from: params.carried_from ?? null
    }

    const payloadBytes = encodePayload(payload as unknown as Record<string, unknown>)
    let envelope: OwnerDomainEnvelope

    try {
      envelope = ports.store.signDomain(BINDING_FORMAT, payloadBytes)
    } catch (error) {
      if (
        error instanceof OwnerKeyError &&
        (error.code === 'anchor_missing' || error.code === 'anchor_mismatch' || error.code === 'no_key')
      ) {
        return { ok: false, reason: 'signing_off' }
      }

      return { ok: false, reason: 'signing_off' }
    }

    const written = writeBindingFile(
      ports.grantsDir ?? defaultOwnerGrantsDir(),
      params.profile,
      params.hermes_session_id,
      envelope,
      ports.fs,
      ports.randomBytes
    )

    if ('reason' in written) {
      return { ok: false, reason: written.reason }
    }

    highWaterMarks.set(key, seq)

    const record: SessionBindingRecord = {
      ok: true,
      state: 'bound',
      profile: params.profile,
      hermes_session_id: params.hermes_session_id,
      seq,
      binding_nonce: nonce,
      bound_at,
      project_root: params.project_root,
      repo_common_root: params.repo_common_root ?? null,
      repo_remote: params.repo_remote ?? null,
      project_id: params.project_id ?? null,
      carried_from: params.carried_from ?? null,
      envelope,
      payload,
      verified: true
    }
    record.record = record
    records.set(key, record)

    return record
  }

  function unbind(profile: string, hermes_session_id: string): SessionBindingOutcome {
    if (!isValidPathComponent(profile) || !isValidPathComponent(hermes_session_id)) {
      return { ok: false, reason: 'bad_input' }
    }

    const key = `${profile}\0${hermes_session_id}`
    const currentHw = highWaterMarks.get(key) ?? 0
    const seq = currentHw + 1

    try {
      ports.store.assertMaySign?.([])
    } catch {
      return { ok: false, reason: 'signing_off' }
    }

    const nonce = generateBindingNonce(ports.randomBytes)
    const bound_at = (ports.now ?? Date.now)()
    const owner_uid = ports.ownerUid ?? (typeof process.getuid === 'function' ? process.getuid() : 501)

    const payload: SessionBindingPayload = {
      v: 1,
      aud: [...BINDING_AUDIENCE],
      owner_uid,
      profile,
      hermes_session_id,
      state: 'unbound',
      seq,
      binding_nonce: nonce,
      bound_at,
      project_root: null,
      repo_common_root: null,
      repo_remote: null,
      project_id: null,
      carried_from: null
    }

    const payloadBytes = encodePayload(payload as unknown as Record<string, unknown>)
    let envelope: OwnerDomainEnvelope

    try {
      envelope = ports.store.signDomain(BINDING_FORMAT, payloadBytes)
    } catch (error) {
      if (
        error instanceof OwnerKeyError &&
        (error.code === 'anchor_missing' || error.code === 'anchor_mismatch' || error.code === 'no_key')
      ) {
        return { ok: false, reason: 'signing_off' }
      }

      return { ok: false, reason: 'signing_off' }
    }

    const written = writeBindingFile(
      ports.grantsDir ?? defaultOwnerGrantsDir(),
      profile,
      hermes_session_id,
      envelope,
      ports.fs,
      ports.randomBytes
    )

    if ('reason' in written) {
      return { ok: false, reason: written.reason }
    }

    highWaterMarks.set(key, seq)

    const record: SessionBindingRecord = {
      ok: true,
      state: 'unbound',
      profile,
      hermes_session_id,
      seq,
      binding_nonce: nonce,
      bound_at,
      project_root: null,
      repo_common_root: null,
      repo_remote: null,
      project_id: null,
      carried_from: null,
      envelope,
      payload,
      verified: true
    }
    record.record = record
    records.set(key, record)

    return record
  }

  function get(profile: string, hermes_session_id: string): SessionBindingRecord | null {
    if (!isValidPathComponent(profile) || !isValidPathComponent(hermes_session_id)) {
      return null
    }

    return records.get(`${profile}\0${hermes_session_id}`) ?? null
  }

  function loadAll(): LoadAllRecords {
    const fs = ports.fs ?? nodeFs
    const grantsDir = ports.grantsDir ?? defaultOwnerGrantsDir()
    const refusedList: RefusedBindingFile[] = []
    const loadedList: SessionBindingRecord[] = []

    if (!fs.existsSync(grantsDir)) {
      return Object.assign(loadedList, { refused: refusedList, records: loadedList, loaded: loadedList })
    }

    let realGrantsDir: string

    try {
      realGrantsDir = fs.realpathSync(grantsDir)
    } catch (err) {
      refusedList.push({ path: grantsDir, reason: 'bad_target', error: err })

      return Object.assign(loadedList, { refused: refusedList, records: loadedList, loaded: loadedList })
    }

    const bindingsDir = path.join(realGrantsDir, 'session-bindings')

    if (!fs.existsSync(bindingsDir)) {
      return Object.assign(loadedList, { refused: refusedList, records: loadedList, loaded: loadedList })
    }

    try {
      const st = fs.lstatSync(bindingsDir)

      if (st.isSymbolicLink()) {
        refusedList.push({ path: bindingsDir, reason: 'symlink_refused' })

        return Object.assign(loadedList, { refused: refusedList, records: loadedList, loaded: loadedList })
      }

      if (!st.isDirectory()) {
        refusedList.push({ path: bindingsDir, reason: 'bad_target' })

        return Object.assign(loadedList, { refused: refusedList, records: loadedList, loaded: loadedList })
      }
    } catch (err) {
      refusedList.push({ path: bindingsDir, reason: 'io_error', error: err })

      return Object.assign(loadedList, { refused: refusedList, records: loadedList, loaded: loadedList })
    }

    let profiles: string[] = []

    try {
      profiles = fs.readdirSync(bindingsDir)
    } catch (err) {
      refusedList.push({ path: bindingsDir, reason: 'io_error', error: err })

      return Object.assign(loadedList, { refused: refusedList, records: loadedList, loaded: loadedList })
    }

    for (const profile of profiles) {
      if (profile.startsWith('.')) {
        continue
      }

      const profileDir = path.join(bindingsDir, profile)

      if (!isValidPathComponent(profile)) {
        refusedList.push({ path: profileDir, reason: 'bad_input' })
        continue
      }

      try {
        const st = fs.lstatSync(profileDir)

        if (st.isSymbolicLink()) {
          refusedList.push({ path: profileDir, reason: 'symlink_refused' })
          continue
        }

        if (!st.isDirectory()) {
          continue
        }
      } catch (err) {
        refusedList.push({ path: profileDir, reason: 'io_error', error: err })
        continue
      }

      let files: string[] = []

      try {
        files = fs.readdirSync(profileDir)
      } catch (err) {
        refusedList.push({ path: profileDir, reason: 'io_error', error: err })
        continue
      }

      for (const file of files) {
        if (file.startsWith('.') || !file.endsWith('.json')) {
          continue
        }

        const filePath = path.join(profileDir, file)

        try {
          const st = fs.lstatSync(filePath)

          if (st.isSymbolicLink()) {
            refusedList.push({ path: filePath, reason: 'symlink_refused' })
            continue
          }

          if (!st.isFile()) {
            continue
          }
        } catch (err) {
          refusedList.push({ path: filePath, reason: 'io_error', error: err })
          continue
        }

        const hermesSessionId = file.slice(0, -5)

        if (!isValidPathComponent(hermesSessionId)) {
          refusedList.push({ path: filePath, reason: 'bad_input' })
          continue
        }

        let content: string

        try {
          content = fs.readFileSync(filePath, 'utf8')
        } catch (err) {
          refusedList.push({ path: filePath, reason: 'io_error', error: err })
          continue
        }

        let envelope: any

        try {
          envelope = JSON.parse(content)
        } catch (err) {
          refusedList.push({ path: filePath, reason: 'malformed', error: err })
          continue
        }

        const verdict = verifyBindingEnvelope(envelope, profile, hermesSessionId, ports)

        if ('reason' in verdict) {
          refusedList.push({ path: filePath, reason: verdict.reason })
          continue
        }

        const key = `${profile}\0${hermesSessionId}`
        const currentHw = highWaterMarks.get(key) ?? 0
        const held = records.get(key)

        // Re-runnable: a reload may re-read the exact record this run already holds (same seq and
        // nonce), e.g. to re-verify it once the owner key and anchor are loaded. Anything older than
        // the in-run high-water, or a different record at the same seq, is a rollback (§4).
        const sameAsHeld = verdict.payload.seq === currentHw && held !== undefined && held.binding_nonce === verdict.payload.binding_nonce

        if (verdict.payload.seq < currentHw || (verdict.payload.seq === currentHw && !sameAsHeld)) {
          refusedList.push({ path: filePath, reason: 'seq_low' })
          continue
        }

        highWaterMarks.set(key, verdict.payload.seq)

        const recordState: SessionBindingRecordState =
          verdict.state === 'verified' ? verdict.payload.state : 'needs_reconfirm'

        const record: SessionBindingRecord = {
          ok: true,
          state: recordState,
          profile,
          hermes_session_id: hermesSessionId,
          seq: verdict.payload.seq,
          binding_nonce: verdict.payload.binding_nonce,
          bound_at: verdict.payload.bound_at,
          project_root: verdict.payload.project_root,
          repo_common_root: verdict.payload.repo_common_root,
          repo_remote: verdict.payload.repo_remote,
          project_id: verdict.payload.project_id,
          carried_from: verdict.payload.carried_from,
          envelope: verdict.envelope,
          payload: verdict.payload,
          verified: verdict.state === 'verified'
        }
        record.record = record
        records.set(key, record)
        loadedList.push(record)
      }
    }

    return Object.assign(loadedList, { refused: refusedList, records: loadedList, loaded: loadedList })
  }

  function resignAll(): ResignAllRecords {
    const fs = ports.fs ?? nodeFs
    const grantsDir = ports.grantsDir ?? defaultOwnerGrantsDir()
    const resignedList: SessionBindingRecord[] = []

    for (const record of records.values()) {
      if (!record.verified || (record.state !== 'bound' && record.state !== 'unbound')) {
        continue
      }

      const payloadBytes = encodePayload(record.payload as unknown as Record<string, unknown>)
      let newEnvelope: OwnerDomainEnvelope

      try {
        newEnvelope = ports.store.signDomain(BINDING_FORMAT, payloadBytes)
      } catch {
        continue
      }

      const written = writeBindingFile(
        grantsDir,
        record.profile,
        record.hermes_session_id,
        newEnvelope,
        fs,
        ports.randomBytes
      )

      if (written.ok) {
        record.envelope = newEnvelope
        resignedList.push(record)
      }
    }

    return Object.assign(resignedList, { resigned: resignedList.length, records: resignedList })
  }

  return {
    bind,
    unbind,
    get,
    loadAll,
    resignAll
  }
}

export function resignAll(store: SessionBindingStore): ResignAllRecords {
  return store.resignAll()
}
