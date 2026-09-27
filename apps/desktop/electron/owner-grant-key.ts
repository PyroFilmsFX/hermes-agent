/**
 * The owner's long-lived Ed25519 grant key, held only in Electron main (#60 U10; VERIFY
 * addendum §1.2, §1.4 step 4, §1.5; tests E-5, E-6).
 *
 * - Generated once, the first time the owner enables owner grants (`ensure()`), with
 *   `crypto.generateKeyPairSync('ed25519')`.
 * - At rest only as a `safeStorage` wrap of the PKCS#8 DER (base64), inside a small JSON blob at
 *   `<real home>/.hermes/owner-grants/.key/owner-key.v1.enc` (file 0600, dir 0700). The wrapping
 *   key is the app's Keychain "Safe Storage" item. There is no plaintext fallback: without
 *   Keychain encryption there is no owner key, and signing stays off.
 * - Loaded at launch only when a blob already exists (`loadIfEnrolled()`), so a machine that
 *   never enabled the feature gets no Keychain touch (and no Keychain prompt) at startup.
 * - The private `KeyObject` lives in a JS private field. Nothing here returns, logs, serializes
 *   or puts in the environment any private or wrapped key byte: `toJSON`, `util.inspect`,
 *   `publicInfo`, `statusForIpc`, `grantKeysEnvValue`, errors and log lines carry the kid and
 *   the public key only (E-6). This module never imports electron: main injects `safeStorage`.
 * - `kid` = `ok_` + the first 16 hex chars of sha256(raw 32-byte public key), exactly
 *   `hermes_owner_grant.envelope.kid_for_pub`.
 * - Signing always prepends the `hermes-owner-grant/v1\0` domain prefix, so this key can't be
 *   used as a general signing oracle. Conductor-scoped signing needs the root-owned anchor's
 *   ACTIVE key to be this key (§1.4 step 4); quote-only grants (plain forwards) still sign, since
 *   backends verify them through `HERMES_OWNER_GRANT_KEYS`.
 * - The blob records the public key and kid in clear. On load the unwrapped private key must
 *   derive that same public key, which catches a blob stitched together from two keys; a
 *   wholesale swapped blob shows up as an anchor mismatch instead (T-4).
 */

import {
  createHash,
  createPrivateKey,
  createPublicKey,
  generateKeyPairSync,
  type KeyObject,
  randomBytes,
  sign
} from 'node:crypto'
import nodeFs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { inspect } from 'node:util'

export const GRANT_FORMAT = 'hermes-owner-grant/v1'
export const GRANT_DOMAIN_PREFIX = Buffer.concat([Buffer.from(GRANT_FORMAT, 'ascii'), Buffer.from([0])])
export const OWNER_KEY_FILE = 'owner-key.v1.enc'
export const OWNER_KEY_BLOB_FORMAT = 'hermes-owner-key/v1'
export const KID_PREFIX = 'ok_'
export const MAX_PAYLOAD_BYTES = 32 * 1024
const MAX_BLOB_BYTES = 64 * 1024
const PUB_LEN = 32

/** The slice of Electron's `safeStorage` this module uses (injected; tests pass a mock). */
export interface SafeStorageLike {
  isEncryptionAvailable(): boolean
  encryptString(plainText: string): Buffer
  decryptString(encrypted: Buffer): string
}

export type OwnerKeyLog = (level: 'info' | 'warn' | 'error', message: string, meta?: Record<string, unknown>) => void

export type OwnerKeyErrorCode =
  | 'anchor_mismatch'
  | 'anchor_missing'
  | 'bad_payload'
  | 'key_blob_corrupt'
  | 'key_blob_inconsistent'
  | 'key_blob_untrusted'
  | 'key_unwrap_failed'
  | 'keychain_unavailable'
  | 'no_key'

export class OwnerKeyError extends Error {
  readonly code: OwnerKeyErrorCode

  constructor(code: OwnerKeyErrorCode, message: string) {
    super(`${code}: ${message}`)
    this.name = 'OwnerKeyError'
    this.code = code
  }
}

export interface OwnerKeyPublic {
  kid: string
  /** base64url (unpadded) of the raw 32-byte Ed25519 public key. */
  pub: string
}

export type AnchorState = 'match' | 'mismatch' | 'missing'

/** The parts of a parsed `hermes-owner-anchor/v1` this module looks at (U11 reads the file). */
export interface AnchorView {
  keys: ReadonlyArray<{ kid: string; pub: string; status: string }>
}

export interface OwnerGrantEnvelope {
  format: typeof GRANT_FORMAT
  kid: string
  payload: string
  sig: string
}

export interface OwnerKeyStatus {
  enrolled: boolean
  kid: string | null
  pub: string | null
  anchor: AnchorState
}

export interface OwnerKeyStoreOptions {
  safeStorage: SafeStorageLike
  /** Directory holding the wrapped blob. Production: `defaultOwnerKeyDir()`. */
  keyDir: string
  fs?: typeof nodeFs
  log?: OwnerKeyLog
  /** The uid that must own the blob (defaults to this process's uid). */
  uid?: number
}

// -- helpers shared with the signing core ----------------------------------------------------

/** The real home directory from the password database: never `$HOME`, never `HERMES_HOME`
 *  (grants belong to the owner, not a profile; addendum D-19). */
export function realHomeDir(): string {
  return os.userInfo().homedir
}

export function defaultOwnerGrantsDir(home: string = realHomeDir()): string {
  return path.join(home, '.hermes', 'owner-grants')
}

export function defaultOwnerKeyDir(home: string = realHomeDir()): string {
  return path.join(defaultOwnerGrantsDir(home), '.key')
}

export function b64urlEncode(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString('base64url')
}

/** Strict unpadded base64url: one accepted spelling per byte string (mirrors envelope.py). */
export function b64urlDecode(text: string): Buffer | null {
  if (typeof text !== 'string' || !/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) {
    return null
  }

  const bytes = Buffer.from(text, 'base64url')

  return bytes.toString('base64url') === text ? bytes : null
}

export function kidForPub(pub: Uint8Array): string {
  if (!(pub instanceof Uint8Array) || pub.length !== PUB_LEN) {
    throw new Error('an Ed25519 public key is 32 bytes')
  }

  return KID_PREFIX + createHash('sha256').update(pub).digest('hex').slice(0, 16)
}

function rawPublicKeyB64url(key: KeyObject): string {
  const jwk = key.export({ format: 'jwk' }) as { crv?: string; x?: string }

  if (jwk.crv !== 'Ed25519' || typeof jwk.x !== 'string') {
    throw new OwnerKeyError('key_blob_corrupt', 'not an Ed25519 key')
  }

  return jwk.x
}

/** Every `scope` a payload declares, read without trusting anything else in it. */
function declaredScopes(payload: Uint8Array): string[] {
  try {
    const obj = JSON.parse(Buffer.from(payload).toString('utf8'))
    const scope = obj && typeof obj === 'object' && !Array.isArray(obj) ? obj.scope : undefined

    return Array.isArray(scope) ? scope.filter((s): s is string => typeof s === 'string') : []
  } catch {
    return []
  }
}

// -- the store ---------------------------------------------------------------------------------

interface BlobDoc {
  format: string
  kid: string
  pub: string
  created_at: number
  wrapped: string
}

class OwnerKeyStoreImpl {
  readonly #safeStorage: SafeStorageLike
  readonly #keyDir: string
  readonly #fs: typeof nodeFs
  readonly #log: OwnerKeyLog
  readonly #uid: number | null
  #privateKey: KeyObject | null = null
  #public: OwnerKeyPublic | null = null
  #anchor: AnchorView | null = null

  constructor(opts: OwnerKeyStoreOptions) {
    this.#safeStorage = opts.safeStorage
    this.#keyDir = opts.keyDir
    this.#fs = opts.fs ?? nodeFs
    this.#log = opts.log ?? (() => {})
    this.#uid = typeof opts.uid === 'number' ? opts.uid : (process.getuid?.() ?? null)
  }

  get blobPath(): string {
    return path.join(this.#keyDir, OWNER_KEY_FILE)
  }

  /** Launch path: load the key when the owner enrolled before; otherwise do nothing (and never
   *  touch the Keychain). Throws `OwnerKeyError` when a blob exists but can't be trusted. */
  loadIfEnrolled(): OwnerKeyPublic | null {
    if (this.#public) {
      return { ...this.#public }
    }

    const doc = this.#readBlob()

    if (!doc) {
      return null
    }

    this.#adopt(doc)

    return { ...this.#public! }
  }

  /** Enable path: load the existing key, or generate, wrap and persist a new one. An existing
   *  blob that fails to load is an error, never a reason to overwrite it with a fresh key. */
  ensure(): OwnerKeyPublic {
    const loaded = this.loadIfEnrolled()

    if (loaded) {
      return loaded
    }

    if (!this.#encryptionAvailable()) {
      throw new OwnerKeyError('keychain_unavailable', 'Keychain encryption is unavailable; the owner key is not created')
    }

    const { privateKey, publicKey } = generateKeyPairSync('ed25519')
    const der = privateKey.export({ format: 'der', type: 'pkcs8' }) as Buffer
    let wrapped: Buffer

    try {
      wrapped = this.#safeStorage.encryptString(der.toString('base64'))
    } catch {
      throw new OwnerKeyError('keychain_unavailable', 'Keychain encryption failed; the owner key is not created')
    } finally {
      der.fill(0)
    }

    const pub = rawPublicKeyB64url(publicKey)
    const kid = kidForPub(Buffer.from(pub, 'base64url'))

    const doc: BlobDoc = {
      format: OWNER_KEY_BLOB_FORMAT,
      kid,
      pub,
      created_at: Date.now(),
      wrapped: Buffer.from(wrapped).toString('base64')
    }

    if (!this.#writeBlobExclusive(JSON.stringify(doc))) {
      // Another writer won the race: trust only what is on disk now.
      return this.loadIfEnrolled() ?? this.#fail('key_blob_corrupt', 'the owner key blob vanished during creation')
    }

    this.#privateKey = privateKey
    this.#public = { kid, pub }
    this.#log('info', 'owner-grant key created', { kid })

    return { kid, pub }
  }

  publicInfo(): OwnerKeyPublic | null {
    return this.#public ? { ...this.#public } : null
  }

  /** One `HERMES_OWNER_GRANT_KEYS` entry: `<kid>:<b64url pub>`. Public by construction. */
  grantKeysEnvValue(): string | null {
    return this.#public ? `${this.#public.kid}:${this.#public.pub}` : null
  }

  /** Record the root-owned anchor as U11 read it (or null when absent/untrusted). */
  setAnchor(anchor: AnchorView | null): AnchorState {
    this.#anchor = anchor && Array.isArray(anchor.keys) ? { keys: anchor.keys.map(k => ({ ...k })) } : null

    return this.anchorState()
  }

  anchorState(): AnchorState {
    if (!this.#anchor) {
      return 'missing'
    }

    const active = this.#anchor.keys.filter(k => k.status === 'active')

    return this.#public && active.length === 1 && active[0].kid === this.#public.kid && active[0].pub === this.#public.pub
      ? 'match'
      : 'mismatch'
  }

  /** Throws unless this key may sign a grant carrying `scopes`. */
  assertMaySign(scopes: readonly string[]): void {
    if (!this.#privateKey || !this.#public) {
      throw new OwnerKeyError('no_key', 'no owner key is loaded')
    }

    if (scopes.length === 0) {
      return
    }

    const state = this.anchorState()

    if (state === 'missing') {
      throw new OwnerKeyError('anchor_missing', 'conductor-scoped grants need the owner-grant anchor installed')
    }

    if (state === 'mismatch') {
      throw new OwnerKeyError('anchor_mismatch', "the owner-grant anchor doesn't match this app's key")
    }
  }

  /** Sign exact payload bytes as a `hermes-owner-grant/v1` envelope. The anchor gate uses the
   *  caller's scopes AND any `scope` the payload itself declares, whichever is wider. */
  signEnvelope(payload: Uint8Array, scopes: readonly string[]): OwnerGrantEnvelope {
    if (!(payload instanceof Uint8Array) || payload.length === 0 || payload.length > MAX_PAYLOAD_BYTES) {
      throw new OwnerKeyError('bad_payload', `payload must be 1..${MAX_PAYLOAD_BYTES} bytes`)
    }

    this.assertMaySign([...new Set([...scopes, ...declaredScopes(payload)])])
    const bytes = Buffer.from(payload)
    const sig = sign(null, Buffer.concat([GRANT_DOMAIN_PREFIX, bytes]), this.#privateKey!)

    return { format: GRANT_FORMAT, kid: this.#public!.kid, payload: b64urlEncode(bytes), sig: b64urlEncode(sig) }
  }

  /** The only shape any future IPC reply may carry. */
  statusForIpc(): OwnerKeyStatus {
    return {
      enrolled: Boolean(this.#public),
      kid: this.#public?.kid ?? null,
      pub: this.#public?.pub ?? null,
      anchor: this.anchorState()
    }
  }

  toJSON(): OwnerKeyStatus {
    return this.statusForIpc()
  }

  [inspect.custom](): string {
    return `OwnerKeyStore ${JSON.stringify(this.statusForIpc())}`
  }

  // -- private ---------------------------------------------------------------------------------

  #fail(code: OwnerKeyErrorCode, message: string): never {
    this.#log('error', 'owner-grant key refused', { code })

    throw new OwnerKeyError(code, message)
  }

  #encryptionAvailable(): boolean {
    try {
      return Boolean(this.#safeStorage.isEncryptionAvailable())
    } catch {
      return false
    }
  }

  #readBlob(): BlobDoc | null {
    const fs = this.#fs
    let st: nodeFs.Stats

    try {
      st = fs.lstatSync(this.blobPath)
    } catch (error: any) {
      if (error?.code === 'ENOENT' || error?.code === 'ENOTDIR') {
        return null
      }

      return this.#fail('key_blob_untrusted', 'the owner key blob cannot be inspected')
    }

    if (st.isSymbolicLink() || !st.isFile()) {
      return this.#fail('key_blob_untrusted', 'the owner key blob is not a regular file')
    }

    if (this.#uid !== null && st.uid !== this.#uid) {
      return this.#fail('key_blob_untrusted', 'the owner key blob is owned by another user')
    }

    if (st.size > MAX_BLOB_BYTES) {
      return this.#fail('key_blob_corrupt', 'the owner key blob is too large')
    }

    const fd = fs.openSync(this.blobPath, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0))
    let text: string

    try {
      const fst = fs.fstatSync(fd)

      if (fst.ino !== st.ino || fst.dev !== st.dev) {
        return this.#fail('key_blob_untrusted', 'the owner key blob changed while it was opened')
      }

      if ((fst.mode & 0o077) !== 0) {
        fs.fchmodSync(fd, 0o600)
        this.#log('warn', 'owner-grant key blob mode tightened to 0600', { was: (fst.mode & 0o777).toString(8) })
      }

      text = fs.readFileSync(fd, 'utf8')
    } finally {
      fs.closeSync(fd)
    }

    let doc: any

    try {
      doc = JSON.parse(text)
    } catch {
      return this.#fail('key_blob_corrupt', 'the owner key blob is not JSON')
    }

    const pub = typeof doc?.pub === 'string' ? b64urlDecode(doc.pub) : null

    if (
      !doc ||
      typeof doc !== 'object' ||
      doc.format !== OWNER_KEY_BLOB_FORMAT ||
      !pub ||
      pub.length !== PUB_LEN ||
      doc.kid !== kidForPub(pub) ||
      typeof doc.wrapped !== 'string' ||
      !/^[A-Za-z0-9+/]+={0,2}$/.test(doc.wrapped)
    ) {
      return this.#fail('key_blob_corrupt', 'the owner key blob is malformed')
    }

    return doc as BlobDoc
  }

  #adopt(doc: BlobDoc): void {
    if (!this.#encryptionAvailable()) {
      this.#fail('keychain_unavailable', 'Keychain encryption is unavailable; the owner key stays locked')
    }

    let pkcs8B64: string

    try {
      pkcs8B64 = this.#safeStorage.decryptString(Buffer.from(doc.wrapped, 'base64'))
    } catch {
      return this.#fail('key_unwrap_failed', 'the Keychain could not unwrap the owner key')
    }

    const der = Buffer.from(String(pkcs8B64 || ''), 'base64')
    let privateKey: KeyObject

    try {
      privateKey = createPrivateKey({ key: der, format: 'der', type: 'pkcs8' })
    } catch {
      return this.#fail('key_blob_corrupt', 'the unwrapped owner key is not a PKCS#8 key')
    } finally {
      der.fill(0)
    }

    if (privateKey.asymmetricKeyType !== 'ed25519') {
      this.#fail('key_blob_corrupt', 'the unwrapped owner key is not Ed25519')
    }

    const pub = rawPublicKeyB64url(createPublicKey(privateKey))

    if (pub !== doc.pub) {
      this.#fail('key_blob_inconsistent', 'the unwrapped owner key does not match the recorded public key')
    }

    this.#privateKey = privateKey
    this.#public = { kid: doc.kid, pub }
    this.#log('info', 'owner-grant key loaded', { kid: doc.kid })
  }

  /** Write the blob with no-clobber semantics: temp file (O_EXCL, 0600) → hard link to the final
   *  name (fails if it exists) → drop the temp name. Returns false when the final name exists. */
  #writeBlobExclusive(text: string): boolean {
    const fs = this.#fs
    fs.mkdirSync(this.#keyDir, { recursive: true, mode: 0o700 })
    const dirStat = fs.lstatSync(this.#keyDir)

    if (!dirStat.isDirectory() || (this.#uid !== null && dirStat.uid !== this.#uid)) {
      this.#fail('key_blob_untrusted', 'the owner key directory is not a directory owned by this user')
    }

    fs.chmodSync(this.#keyDir, 0o700)
    const tmp = path.join(this.#keyDir, `.${OWNER_KEY_FILE}.${process.pid}.${randomBytes(6).toString('hex')}.tmp`)
    const fd = fs.openSync(tmp, 'wx', 0o600)

    try {
      fs.writeSync(fd, text)
      fs.fsyncSync(fd)
    } finally {
      fs.closeSync(fd)
    }

    try {
      fs.linkSync(tmp, this.blobPath)

      return true
    } catch (error: any) {
      if (error?.code === 'EEXIST') {
        return false
      }

      throw error
    } finally {
      fs.unlinkSync(tmp)
    }
  }
}

export type OwnerKeyStore = OwnerKeyStoreImpl

export function createOwnerKeyStore(opts: OwnerKeyStoreOptions): OwnerKeyStore {
  return new OwnerKeyStoreImpl(opts)
}
