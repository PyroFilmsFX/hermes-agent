/**
 * #60 review P1 (2026-09-27): Electron reads the root-owned owner-grant anchor with the same
 * StrictModes checks as `hermes_owner_grant.anchor.load_trusted_anchor`, and the key store only
 * signs with the anchor's active key.
 *
 * Nothing here touches /Library: the reader takes an injected fake fs that answers for the
 * real, hard-coded path (the path itself is not a parameter).
 */

import { createHash } from 'node:crypto'

import { describe, expect, test } from 'vitest'

import { OWNER_ANCHOR_PATH, parseOwnerAnchor, readTrustedOwnerAnchor, type OwnerAnchorFs } from './owner-grant-anchor'

const DIR = '/Library/Application Support/Hermes/owner-grant'
const CHAIN = ['/', '/Library', '/Library/Application Support', '/Library/Application Support/Hermes', DIR]
const UID = 501

const S_IFDIR = 0o040000
const S_IFREG = 0o100000
const S_IFLNK = 0o120000

function b64u(bytes: Buffer): string {
  return bytes.toString('base64url')
}

function kid(pub: Buffer): string {
  return 'ok_' + createHash('sha256').update(pub).digest('hex').slice(0, 16)
}

const PUB_A = Buffer.from(Array.from({ length: 32 }, (_, i) => i))
const PUB_B = Buffer.from(Array.from({ length: 32 }, (_, i) => i + 1))

function key(pub: Buffer, status = 'active', extra: Record<string, unknown> = {}) {
  return {
    kid: kid(pub),
    alg: 'Ed25519',
    pub: b64u(pub),
    status,
    not_before: 0,
    retired_at: status === 'retired' ? 5 : null,
    ...extra
  }
}

function doc(overrides: Record<string, unknown> = {}) {
  return {
    format: 'hermes-owner-anchor/v1',
    owner_uid: UID,
    grants_dir: '/Users/owner/.hermes/owner-grants',
    keys: [key(PUB_A)],
    ...overrides
  }
}

interface Entry {
  kind: number
  uid: number
  perm: number
  ino: number
}

class FakeFs implements OwnerAnchorFs {
  entries = new Map<string, Entry>()
  content: Buffer | null
  swappedIno: number | null = null
  touched: string[] = []
  constants = { O_RDONLY: 0, O_NOFOLLOW: 0x100, O_NONBLOCK: 0x4 }

  constructor(content: unknown = doc()) {
    CHAIN.forEach((p, i) => this.entries.set(p, { kind: S_IFDIR, uid: 0, perm: 0o755, ino: 10 + i }))
    this.content = Buffer.from(typeof content === 'string' ? content : JSON.stringify(content), 'utf8')
    this.entries.set(OWNER_ANCHOR_PATH, { kind: S_IFREG, uid: 0, perm: 0o644, ino: 99 })
  }

  #stat(e: Entry, size = 0) {
    return {
      uid: e.uid,
      mode: e.kind | e.perm,
      dev: 1,
      ino: e.ino,
      size,
      isSymbolicLink: () => e.kind === S_IFLNK,
      isDirectory: () => e.kind === S_IFDIR,
      isFile: () => e.kind === S_IFREG
    }
  }

  lstatSync(p: string) {
    this.touched.push(p)
    const e = this.entries.get(p)

    if (!e) {
      throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' })
    }

    return this.#stat(e, p === OWNER_ANCHOR_PATH ? (this.content?.length ?? 0) : 0)
  }

  openSync(p: string, flags: number) {
    this.touched.push(p)
    expect(flags & this.constants.O_NOFOLLOW).toBeTruthy()

    if (!this.entries.has(p)) {
      throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' })
    }

    return 7
  }

  fstatSync(_fd: number) {
    const e = this.entries.get(OWNER_ANCHOR_PATH)!

    return this.#stat(this.swappedIno === null ? e : { ...e, ino: this.swappedIno }, this.content?.length ?? 0)
  }

  readFileSync(_fd: number) {
    return this.content ?? Buffer.alloc(0)
  }

  closeSync(_fd: number) {}
}

function read(fs: FakeFs, uid = UID) {
  return readTrustedOwnerAnchor({ fs, uid })
}

describe('the owner-grant anchor reader (StrictModes, mirrors hermes_owner_grant.anchor)', () => {
  test('the path is the hard-coded root-owned location', () => {
    expect(OWNER_ANCHOR_PATH).toBe(`${DIR}/anchor.json`)
  })

  test('a root-owned chain and file loads, exposing the active key', () => {
    const fs = new FakeFs(doc({ keys: [key(PUB_A), key(PUB_B, 'retired')] }))
    const res = read(fs)

    expect(res.ok).toBe(true)

    if (res.ok) {
      expect(res.anchor.ownerUid).toBe(UID)
      expect(res.anchor.keys.map(k => [k.kid, k.status])).toEqual([
        [kid(PUB_A), 'active'],
        [kid(PUB_B), 'retired']
      ])
    }

    for (const p of CHAIN) {
      expect(fs.touched).toContain(p)
    }
  })

  test('a missing anchor is anchor_missing', () => {
    const fs = new FakeFs()
    fs.entries.delete(OWNER_ANCHOR_PATH)
    expect(read(fs)).toMatchObject({ ok: false, reason: 'anchor_missing' })
  })

  test.each([
    ['file owned by the user', (fs: FakeFs) => (fs.entries.get(OWNER_ANCHOR_PATH)!.uid = UID)],
    ['file group-writable', (fs: FakeFs) => (fs.entries.get(OWNER_ANCHOR_PATH)!.perm = 0o664)],
    ['file is a symlink', (fs: FakeFs) => (fs.entries.get(OWNER_ANCHOR_PATH)!.kind = S_IFLNK)],
    ['a parent dir owned by the user', (fs: FakeFs) => (fs.entries.get('/Library/Application Support/Hermes')!.uid = UID)],
    ['a parent dir other-writable', (fs: FakeFs) => (fs.entries.get(DIR)!.perm = 0o757)],
    ['a parent dir is a symlink', (fs: FakeFs) => (fs.entries.get(DIR)!.kind = S_IFLNK)],
    ['swapped between lstat and open', (fs: FakeFs) => (fs.swappedIno = 1234)]
  ])('untrusted: %s', (_name, mutate) => {
    const fs = new FakeFs()
    mutate(fs)
    expect(read(fs)).toMatchObject({ ok: false, reason: 'anchor_untrusted' })
  })

  test.each([
    ['anchor for another uid', doc({ owner_uid: UID + 1 })],
    ['kid not derived from pub', doc({ keys: [key(PUB_A, 'active', { kid: kid(PUB_B) })] })],
    ['two active keys', doc({ keys: [key(PUB_A), key(PUB_B)] })],
    ['unknown status', doc({ keys: [key(PUB_A, 'trusted')] })],
    ['extra key field', doc({ keys: [key(PUB_A, 'active', { note: 'x' })] })],
    ['wrong format', doc({ format: 'hermes-owner-anchor/v2' })],
    ['no keys', doc({ keys: [] })],
    ['not JSON', 'not json']
  ])('untrusted content: %s', (_name, content) => {
    expect(read(new FakeFs(content))).toMatchObject({ ok: false, reason: 'anchor_untrusted' })
  })

  test('parseOwnerAnchor is the content half (no filesystem)', () => {
    const anchor = parseOwnerAnchor(Buffer.from(JSON.stringify(doc())))
    expect(anchor.keys[0]).toMatchObject({ kid: kid(PUB_A), pub: b64u(PUB_A), status: 'active' })
    expect(() => parseOwnerAnchor(Buffer.from('{}'))).toThrow()
  })
})
