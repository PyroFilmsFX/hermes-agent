/**
 * The root-owned owner-grant trust anchor, read by Electron main (#60 review P1, 2026-09-27;
 * VERIFY addendum §1.3, §1.5, T-4). The TypeScript twin of `hermes_owner_grant/anchor.py`.
 *
 * The anchor is the ONLY thing that says which owner key is trusted. Anything under
 * `~/.hermes` (the wrapped key blob included) is agent-writable, so the key store signs only
 * with the key the anchor lists as active (`OwnerKeyStore.setAnchor`).
 *
 * Before trusting the file this applies ssh `StrictModes`-style checks to the file and every
 * directory from `/` down to it: `lstat` shows no symlink and the right kind, `uid === 0`, and no
 * group- or other-write bit. The file is then opened with `O_NOFOLLOW` and its `fstat` must be
 * the same inode and pass the same checks. The path is a literal; there is no parameter, env
 * variable or flag that changes it. Tests inject a filesystem (`fs`) that answers for this path.
 *
 * Known limit (same as the Python loader): macOS ACLs are not inspected. Only root can add an
 * ACL to a root-owned directory.
 */

import nodeFs from 'node:fs'
import path from 'node:path'

import { b64urlDecode, kidForPub } from './owner-grant-key'

export const OWNER_ANCHOR_FORMAT = 'hermes-owner-anchor/v1'
export const OWNER_ANCHOR_DIR = '/Library/Application Support/Hermes/owner-grant'
export const OWNER_ANCHOR_PATH = `${OWNER_ANCHOR_DIR}/anchor.json`
const MAX_ANCHOR_BYTES = 64 * 1024
const MAX_KEYS = 32
const STATUSES = new Set(['active', 'retired', 'revoked'])
const ANCHOR_FIELDS = new Set(['format', 'owner_uid', 'grants_dir', 'keys', 'verifier_sha256'])
const REQUIRED_FIELDS = ['format', 'owner_uid', 'grants_dir', 'keys']
const KEY_FIELDS = ['alg', 'kid', 'not_before', 'pub', 'retired_at', 'status']
const WRITABLE_BY_OTHERS = 0o022

export interface OwnerAnchorKey {
  kid: string
  alg: 'Ed25519'
  pub: string
  status: string
  notBefore: number
  retiredAt: number | null
}

export interface OwnerAnchor {
  ownerUid: number
  grantsDir: string
  keys: OwnerAnchorKey[]
}

export type OwnerAnchorRead =
  | { ok: true; anchor: OwnerAnchor }
  | { ok: false; reason: 'anchor_missing' | 'anchor_untrusted'; detail: string }

interface StatLike {
  uid: number
  mode: number
  dev: number | bigint
  ino: number | bigint
  size: number | bigint
  isSymbolicLink(): boolean
  isDirectory(): boolean
  isFile(): boolean
}

/** The slice of `node:fs` the reader uses; production passes `node:fs`, tests a fake. */
export interface OwnerAnchorFs {
  lstatSync(p: string): StatLike
  openSync(p: string, flags: number): number
  fstatSync(fd: number): StatLike
  readFileSync(fd: number): Buffer
  closeSync(fd: number): void
  constants: { O_RDONLY: number; O_NOFOLLOW?: number; O_NONBLOCK?: number }
}

class AnchorReadError extends Error {
  constructor(
    readonly reason: 'anchor_missing' | 'anchor_untrusted',
    readonly detail: string
  ) {
    super(`${reason}: ${detail}`)
  }
}

function untrusted(detail: string): AnchorReadError {
  return new AnchorReadError('anchor_untrusted', detail)
}

function isInt(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value)
}

function requireRootOwned(st: StatLike, p: string): void {
  if (st.uid !== 0) {
    throw untrusted(`${p} is owned by uid ${st.uid}, not root`)
  }

  if ((st.mode & WRITABLE_BY_OTHERS) !== 0) {
    throw untrusted(`${p} is group- or other-writable`)
  }
}

function lstat(fs: OwnerAnchorFs, p: string): StatLike {
  try {
    return fs.lstatSync(p)
  } catch (error: any) {
    if (error?.code === 'ENOENT' || error?.code === 'ENOTDIR') {
      throw new AnchorReadError('anchor_missing', `${p} does not exist`)
    }

    throw untrusted(`cannot lstat ${p}`)
  }
}

function dirChain(dir: string): string[] {
  const chain = ['/']

  for (const part of dir.split('/').filter(Boolean)) {
    chain.push(path.posix.join(chain[chain.length - 1], part))
  }

  return chain
}

/** Parse anchor bytes (`hermes-owner-anchor/v1`); throws on anything the Python parser refuses. */
export function parseOwnerAnchor(data: Buffer): OwnerAnchor {
  let obj: any

  try {
    obj = JSON.parse(data.toString('utf8'))
  } catch {
    throw untrusted('anchor is not JSON')
  }

  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    throw untrusted('anchor is not an object')
  }

  const fields = Object.keys(obj)

  if (!REQUIRED_FIELDS.every(f => fields.includes(f)) || !fields.every(f => ANCHOR_FIELDS.has(f))) {
    throw untrusted('anchor fields are wrong')
  }

  if (obj.format !== OWNER_ANCHOR_FORMAT) {
    throw untrusted(`format is not ${OWNER_ANCHOR_FORMAT}`)
  }

  if (!isInt(obj.owner_uid) || obj.owner_uid < 0) {
    throw untrusted('owner_uid must be a non-negative int')
  }

  const grantsDir = obj.grants_dir

  if (
    typeof grantsDir !== 'string' ||
    !grantsDir.startsWith('/') ||
    grantsDir === '/' ||
    grantsDir.includes('\0') ||
    path.posix.normalize(grantsDir) !== grantsDir ||
    grantsDir.endsWith('/')
  ) {
    throw untrusted('grants_dir must be a normalized absolute path')
  }

  if (
    obj.verifier_sha256 !== undefined &&
    obj.verifier_sha256 !== null &&
    !(typeof obj.verifier_sha256 === 'string' && /^[0-9a-f]{64}$/.test(obj.verifier_sha256))
  ) {
    throw untrusted('verifier_sha256 must be 64 lowercase hex')
  }

  if (!Array.isArray(obj.keys) || obj.keys.length < 1 || obj.keys.length > MAX_KEYS) {
    throw untrusted(`keys must be a list of 1..${MAX_KEYS} entries`)
  }

  const keys: OwnerAnchorKey[] = obj.keys.map((entry: any, i: number) => {
    const where = `keys[${i}]`

    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      throw untrusted(`${where} is not an object`)
    }

    if (Object.keys(entry).sort().join(',') !== KEY_FIELDS.join(',')) {
      throw untrusted(`${where} must have exactly the fields ${KEY_FIELDS.join(', ')}`)
    }

    if (entry.alg !== 'Ed25519') {
      throw untrusted(`${where}.alg is not Ed25519`)
    }

    const pub = typeof entry.pub === 'string' ? b64urlDecode(entry.pub) : null

    if (!pub || pub.length !== 32 || entry.kid !== kidForPub(pub)) {
      throw untrusted(`${where}.kid does not match its pub`)
    }

    if (!STATUSES.has(entry.status)) {
      throw untrusted(`${where}.status is not active, retired or revoked`)
    }

    if (!isInt(entry.not_before) || entry.not_before < 0) {
      throw untrusted(`${where}.not_before must be a non-negative int`)
    }

    if (entry.retired_at !== null && (!isInt(entry.retired_at) || entry.retired_at < 0)) {
      throw untrusted(`${where}.retired_at must be null or a non-negative int`)
    }

    if (entry.status === 'active' && entry.retired_at !== null) {
      throw untrusted(`${where} is active but has retired_at`)
    }

    if (entry.status === 'retired' && entry.retired_at === null) {
      throw untrusted(`${where} is retired without retired_at`)
    }

    return {
      kid: entry.kid,
      alg: 'Ed25519',
      pub: entry.pub,
      status: entry.status,
      notBefore: entry.not_before,
      retiredAt: entry.retired_at
    }
  })

  if (new Set(keys.map(k => k.kid)).size !== keys.length) {
    throw untrusted('duplicate kid')
  }

  if (keys.filter(k => k.status === 'active').length > 1) {
    throw untrusted('more than one active key')
  }

  return { ownerUid: obj.owner_uid, grantsDir, keys }
}

function loadAt(fs: OwnerAnchorFs): OwnerAnchor {
  for (const dir of dirChain(OWNER_ANCHOR_DIR)) {
    const st = lstat(fs, dir)

    if (st.isSymbolicLink()) {
      throw untrusted(`${dir} is a symlink`)
    }

    if (!st.isDirectory()) {
      throw untrusted(`${dir} is not a directory`)
    }

    requireRootOwned(st, dir)
  }

  const st = lstat(fs, OWNER_ANCHOR_PATH)

  if (st.isSymbolicLink() || !st.isFile()) {
    throw untrusted(`${OWNER_ANCHOR_PATH} is not a regular file`)
  }

  requireRootOwned(st, OWNER_ANCHOR_PATH)
  let fd: number

  try {
    fd = fs.openSync(OWNER_ANCHOR_PATH, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0))
  } catch (error: any) {
    if (error?.code === 'ENOENT') {
      throw new AnchorReadError('anchor_missing', `${OWNER_ANCHOR_PATH} disappeared before open`)
    }

    throw untrusted(`cannot open ${OWNER_ANCHOR_PATH}`)
  }

  let data: Buffer

  try {
    const opened = fs.fstatSync(fd)

    if (opened.dev !== st.dev || opened.ino !== st.ino) {
      throw untrusted(`${OWNER_ANCHOR_PATH} changed between lstat and open`)
    }

    if (!opened.isFile()) {
      throw untrusted(`${OWNER_ANCHOR_PATH} is not a regular file`)
    }

    requireRootOwned(opened, OWNER_ANCHOR_PATH)

    if (Number(opened.size) > MAX_ANCHOR_BYTES) {
      throw untrusted(`${OWNER_ANCHOR_PATH} exceeds ${MAX_ANCHOR_BYTES} bytes`)
    }

    data = fs.readFileSync(fd)
  } finally {
    fs.closeSync(fd)
  }

  if (data.length > MAX_ANCHOR_BYTES) {
    throw untrusted(`${OWNER_ANCHOR_PATH} exceeds ${MAX_ANCHOR_BYTES} bytes`)
  }

  return parseOwnerAnchor(data)
}

/** Read and trust-check the anchor at the hard-coded path. Never throws: a missing or untrusted
 *  anchor is a result, and the caller then signs nothing. `uid` must match the anchor's owner. */
export function readTrustedOwnerAnchor(opts: { fs?: OwnerAnchorFs; uid?: number | null } = {}): OwnerAnchorRead {
  const uid = opts.uid === undefined ? (process.getuid?.() ?? null) : opts.uid

  try {
    const anchor = loadAt(opts.fs ?? (nodeFs as unknown as OwnerAnchorFs))

    if (uid === null || anchor.ownerUid !== uid) {
      throw untrusted(`the anchor belongs to uid ${anchor.ownerUid}, not ${uid}`)
    }

    return { ok: true, anchor }
  } catch (error) {
    if (error instanceof AnchorReadError) {
      return { ok: false, reason: error.reason, detail: error.detail }
    }

    return { ok: false, reason: 'anchor_untrusted', detail: 'the anchor could not be read' }
  }
}
