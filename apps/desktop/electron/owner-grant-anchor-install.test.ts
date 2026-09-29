/**
 * #60 U11: the anchor install / rotate / revoke flow and the launch mismatch refusal (VERIFY
 * addendum §1.4, §1.5; E-9, T-3, T-4; decision D20: the enable path always makes a fresh key).
 *
 * Isolation: nothing here touches the real /Library, the real Keychain, ~/.hermes or ~/.claude,
 * and no admin prompt can appear.
 * - The privileged step is a fake admin runner. It checks the argv is the fixed one and runs the
 *   REAL root script under /bin/sh, with its one constant directory pointed at a mkdtemp that
 *   stands in for /Library, and `chown` swapped for `true`. It never calls osascript.
 * - The anchor reader gets a redirecting fs that answers for the hard-coded anchor path from that
 *   temp dir, and reports root ownership only for what the fake runner installed.
 * - safeStorage is a mock; key dirs are mkdtemps.
 * The one osascript run (E-9e) strips the admin clause first, so it cannot raise a prompt.
 */

import { spawnSync } from 'node:child_process'
import { createHash, createPublicKey, verify as edVerify } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, test } from 'vitest'

import {
  ADMIN_APPLESCRIPT,
  ANCHOR_ROOT_SCRIPT,
  type AdminRunner,
  buildAdminArgv,
  createOsascriptAdminRunner,
  createOwnerGrantController,
  installedVerifierIsCurrent,
  OSASCRIPT_PATH,
  OWNER_VERIFY_LAUNCHER,
  type OwnerGrantConfirmRequest,
  STAGED_PAYLOAD_FILES,
  VERIFIER_PACKAGE_FILES
} from './owner-grant-anchor-install'
import { OWNER_ANCHOR_DIR, OWNER_ANCHOR_PATH, type OwnerAnchorFs, readTrustedOwnerAnchor } from './owner-grant-anchor'
import { createOwnerKeyStore, GRANT_DOMAIN_PREFIX, OWNER_KEY_FILE, type SafeStorageLike } from './owner-grant-key'

const UID = process.getuid!()
const NOW = 1_790_000_000_000
const HERMES_TOP = '/Library/Application Support/Hermes'
const PYTHON = process.env.OWNER_GRANT_TEST_PYTHON || '/tmp/venv314/bin/python'
const REPO = path.resolve(__dirname, '../../..')
const HAVE_PYTHON = fs.existsSync(PYTHON) && fs.existsSync(path.join(REPO, 'hermes_owner_grant', 'anchor.py'))
const VERIFIER_SOURCE_DIR = path.join(REPO, 'hermes_owner_grant')
const HAVE_SYSTEM_PYTHON = process.platform === 'darwin' && fs.existsSync('/usr/bin/python3')
const OWNER_VERIFY_PATH = `${OWNER_ANCHOR_DIR}/hermes_owner_verify.py`
const sha256Hex = (bytes: Buffer | string) => createHash('sha256').update(bytes).digest('hex')

const tmpDirs: string[] = []

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

function mkTmp(prefix: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  tmpDirs.push(dir)

  return dir
}

function mockSafeStorage() {
  const calls: string[] = []

  const api: SafeStorageLike = {
    isEncryptionAvailable: () => {
      calls.push('isEncryptionAvailable')

      return true
    },
    encryptString: (plain: string) => {
      calls.push('encryptString')

      return Buffer.from('MOCKWRAP:' + Buffer.from(plain, 'utf8').reverse().toString('base64'), 'utf8')
    },
    decryptString: (wrapped: Buffer) => {
      calls.push('decryptString')
      const text = wrapped.toString('utf8')

      if (!text.startsWith('MOCKWRAP:')) {
        throw new Error('mock keychain: not a blob this mock wrapped')
      }

      return Buffer.from(text.slice('MOCKWRAP:'.length), 'base64').reverse().toString('utf8')
    }
  }

  return { api, calls }
}

/** Answers for the hard-coded anchor path out of `root` (the stand-in for /). `/`, `/Library`
 *  and `/Library/Application Support` are synthesized root-owned 0755 dirs, so the real ones are
 *  never even lstat'd. A path is root-owned only when the fake admin runner installed it. */
class RedirectFs implements OwnerAnchorFs {
  readonly rootOwned = new Set<string>()
  readonly #fds = new Map<number, string>()
  readonly constants = {
    O_RDONLY: fs.constants.O_RDONLY,
    O_NOFOLLOW: fs.constants.O_NOFOLLOW,
    O_NONBLOCK: fs.constants.O_NONBLOCK
  }

  constructor(readonly root: string) {}

  real(p: string): string {
    if (p !== HERMES_TOP && !p.startsWith(HERMES_TOP + '/')) {
      throw Object.assign(new Error(`RedirectFs: refusing ${p}`), { code: 'ENOENT' })
    }

    return path.join(this.root, p)
  }

  #wrap(st: fs.Stats, virtual: string) {
    return {
      uid: this.rootOwned.has(virtual) ? 0 : st.uid,
      mode: st.mode,
      dev: st.dev,
      ino: st.ino,
      size: st.size,
      isSymbolicLink: () => st.isSymbolicLink(),
      isDirectory: () => st.isDirectory(),
      isFile: () => st.isFile()
    }
  }

  lstatSync(p: string) {
    if (p === '/' || p === '/Library' || p === '/Library/Application Support') {
      return {
        uid: 0,
        mode: 0o040755,
        dev: 1,
        ino: p.length,
        size: 0,
        isSymbolicLink: () => false,
        isDirectory: () => true,
        isFile: () => false
      }
    }

    return this.#wrap(fs.lstatSync(this.real(p)), p)
  }

  openSync(p: string, flags: number): number {
    const fd = fs.openSync(this.real(p), flags)
    this.#fds.set(fd, p)

    return fd
  }

  fstatSync(fd: number) {
    return this.#wrap(fs.fstatSync(fd), this.#fds.get(fd)!)
  }

  readFileSync(fd: number): Buffer {
    return fs.readFileSync(fd)
  }

  closeSync(fd: number): void {
    this.#fds.delete(fd)
    fs.closeSync(fd)
  }
}

/** The real root script, retargeted at the fake /Library and with chown neutralized. The top
 *  line and the warm-step guard must each hit exactly once, so a drifting script fails loudly.
 *  The warm step (pycache/, ~1 s of py_compile) runs only when a test asks for it. */
function retargetedRootScript(fakeRoot: string, warm = false): string {
  const topLine = `top='${HERMES_TOP}'`
  const warmGuard = 'if /usr/bin/xcode-select -p >/dev/null 2>&1; then'
  expect(ANCHOR_ROOT_SCRIPT.split(topLine).length).toBe(2)
  expect(ANCHOR_ROOT_SCRIPT.split(warmGuard).length).toBe(2)
  expect(ANCHOR_ROOT_SCRIPT).toContain('/usr/sbin/chown root:wheel')

  return ANCHOR_ROOT_SCRIPT.replace(topLine, `top='${fakeRoot}${HERMES_TOP}'`)
    .replace(warmGuard, warm ? warmGuard : 'if false; then')
    .replaceAll('/usr/sbin/chown ', '/usr/bin/true ')
}

/** Every path under `dir`, relative, sorted; directories end with '/'. */
function walk(dir: string, rel = ''): string[] {
  const out: string[] = []

  for (const name of fs.readdirSync(path.join(dir, rel)).sort()) {
    const r = rel ? `${rel}/${name}` : name
    const st = fs.lstatSync(path.join(dir, r))

    if (st.isDirectory()) {
      out.push(`${r}/`, ...walk(dir, r))
    } else {
      out.push(r)
    }
  }

  return out
}

interface StagedEntry {
  mode: number
  content: Buffer | null
}

interface RunnerSeen {
  argv: string[]
  srcMode: number
  srcDirMode: number
  srcContent: Buffer
  staged: Record<string, StagedEntry>
}

/** A fake admin runner: the argv must be the fixed one; then the real root script runs. */
function fakeAdminRunner(rfs: RedirectFs, opts: { before?: (src: string) => void; warm?: boolean } = {}) {
  const seen: RunnerSeen[] = []
  const expectedHead = ['-e', ADMIN_APPLESCRIPT[0], '-e', ADMIN_APPLESCRIPT[1], '-e', ADMIN_APPLESCRIPT[2], ANCHOR_ROOT_SCRIPT]

  const runner: AdminRunner = {
    run: async argv => {
      expect(argv.slice(0, 7)).toEqual(expectedHead)
      expect(argv).toHaveLength(9)
      const [src, sha] = [argv[7], argv[8]]
      const stagedDir = path.dirname(src)
      const staged: Record<string, StagedEntry> = {}

      for (const rel of walk(stagedDir)) {
        const full = path.join(stagedDir, rel)
        staged[rel] = {
          mode: fs.statSync(full).mode & 0o777,
          content: rel.endsWith('/') ? null : fs.readFileSync(full)
        }
      }

      seen.push({
        argv: [...argv],
        srcMode: fs.statSync(src).mode & 0o777,
        srcDirMode: fs.statSync(stagedDir).mode & 0o777,
        srcContent: fs.readFileSync(src),
        staged
      })
      opts.before?.(src)
      const res = spawnSync('/bin/sh', ['-c', retargetedRootScript(rfs.root, opts.warm), 'hermes-owner-anchor', src, sha], {
        encoding: 'utf8'
      })

      if (res.status === 0) {
        rfs.rootOwned.add(HERMES_TOP)

        for (const rel of walk(rfs.real(HERMES_TOP))) {
          rfs.rootOwned.add(`${HERMES_TOP}/${rel.replace(/\/$/, '')}`)
        }
      }

      return { code: res.status ?? 1 }
    }
  }

  return { runner, seen }
}

interface Harness {
  rfs: RedirectFs
  keyDir: string
  ss: ReturnType<typeof mockSafeStorage>
  confirms: Array<OwnerGrantConfirmRequest & { keychainCallsAtConfirm: number }>
  runner: AdminRunner
  seen: RunnerSeen[]
  tmpRoot: string
  controller: ReturnType<typeof createOwnerGrantController>
  store: ReturnType<typeof createOwnerKeyStore>
  readAnchor: () => ReturnType<typeof readTrustedOwnerAnchor>
}

function harness(
  opts: {
    confirm?: boolean
    runner?: (rfs: RedirectFs) => AdminRunner
    keyDir?: string
    rfs?: RedirectFs
    ss?: ReturnType<typeof mockSafeStorage>
    now?: () => number
    before?: (src: string) => void
    verifierSourceDir?: string
    warm?: boolean
    /** b9 §6: read the installed manifest.sha256 out of the fake /Library. */
    manifestReader?: boolean
  } = {}
): Harness {
  const rfs = opts.rfs ?? new RedirectFs(mkTmp('ogai-root-'))
  fs.mkdirSync(path.join(rfs.root, 'Library', 'Application Support'), { recursive: true })
  const keyDir = opts.keyDir ?? path.join(mkTmp('ogai-home-'), 'owner-grants', '.key')
  const ss = opts.ss ?? mockSafeStorage()
  const store = createOwnerKeyStore({ safeStorage: ss.api, keyDir })
  const confirms: Harness['confirms'] = []
  const fake = fakeAdminRunner(rfs, { before: opts.before, warm: opts.warm })
  const runner = opts.runner ? opts.runner(rfs) : fake.runner
  const tmpRoot = mkTmp('ogai-tmp-')
  const readAnchor = () => readTrustedOwnerAnchor({ fs: rfs, uid: UID })

  const controller = createOwnerGrantController({
    store,
    readAnchor,
    adminRunner: runner,
    confirm: async req => {
      confirms.push({ ...req, keychainCallsAtConfirm: ss.calls.filter(c => c !== 'isEncryptionAvailable').length })

      return opts.confirm !== false
    },
    ownerUid: UID,
    grantsDir: '/Users/owner/.hermes/owner-grants',
    tmpRoot,
    verifierSourceDir: opts.verifierSourceDir ?? VERIFIER_SOURCE_DIR,
    ...(opts.manifestReader
      ? {
          readInstalledManifest: () => {
            try {
              return fs.readFileSync(rfs.real(`${OWNER_ANCHOR_DIR}/manifest.sha256`))
            } catch {
              return null
            }
          }
        }
      : {}),
    now: opts.now ?? (() => NOW),
    platform: 'darwin'
  })

  return { rfs, keyDir, ss, confirms, runner, seen: fake.seen, tmpRoot, controller, store, readAnchor }
}

function anchorOnDisk(h: Harness): any {
  return JSON.parse(fs.readFileSync(h.rfs.real(OWNER_ANCHOR_PATH), 'utf8'))
}

function blobKid(keyDir: string): string | null {
  const p = path.join(keyDir, OWNER_KEY_FILE)

  return fs.existsSync(p) ? JSON.parse(fs.readFileSync(p, 'utf8')).kid : null
}

function signsWith(store: Harness['store'], pub: string): boolean {
  const env = store.signEnvelope(Buffer.from('{"v":1}'), [])
  const key = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: pub }, format: 'jwk' })

  return edVerify(
    null,
    Buffer.concat([GRANT_DOMAIN_PREFIX, Buffer.from(env.payload, 'base64url')]),
    key,
    Buffer.from(env.sig, 'base64url')
  )
}

/** A complete, decryptable blob for some other key, planted in `keyDir` (an agent's move). */
function plantBlob(keyDir: string, ss: ReturnType<typeof mockSafeStorage>): { kid: string; pub: string } {
  return createOwnerKeyStore({ safeStorage: ss.api, keyDir }).ensure()
}

function leftovers(dir: string): string[] {
  return fs.existsSync(dir) ? fs.readdirSync(dir) : []
}

// -- E-9: the privileged command is fixed argv; nothing agent-influenced is interpolated ------

describe('E-9: the admin command is a constant; only a validated temp path and a sha256 are arguments', () => {
  test('E-9a the argv is the fixed osascript program + the constant root script + [staged file, sha256]', async () => {
    const a = harness()
    await a.controller.enable()
    const b = harness()
    await b.controller.enable()

    expect(a.seen).toHaveLength(1)
    expect(b.seen).toHaveLength(1)
    const [argvA, argvB] = [a.seen[0].argv, b.seen[0].argv]
    // Two different keys, byte-identical command apart from the temp path and the hash.
    expect(argvA.slice(0, 7)).toEqual(argvB.slice(0, 7))
    expect(argvA[7]).toMatch(/\/hermes-owner-anchor-[A-Za-z0-9]{6}\/manifest\.sha256$/)
    expect(argvA[8]).toMatch(/^[0-9a-f]{64}$/)
    expect(argvA[8]).toBe(createHash('sha256').update(a.seen[0].srcContent).digest('hex'))
    // No key material, kid or anchor JSON rides in the command.
    const kidA = a.store.publicInfo()!.kid
    const pubA = a.store.publicInfo()!.pub

    for (const arg of argvA) {
      expect(arg).not.toContain(kidA)
      expect(arg).not.toContain(pubA)
      expect(arg).not.toContain('hermes-owner-anchor/v1')
    }

    expect(ADMIN_APPLESCRIPT[1]).toContain('with administrator privileges')
    expect(ADMIN_APPLESCRIPT[1]).toContain('quoted form of (item 1 of argv)')
    expect(ADMIN_APPLESCRIPT[1]).toContain('quoted form of (item 2 of argv)')
    expect(ADMIN_APPLESCRIPT[1]).toContain('quoted form of (item 3 of argv)')
    expect(OSASCRIPT_PATH).toBe('/usr/bin/osascript')
  })

  test('E-9b hostile or malformed arguments are refused before any runner runs', () => {
    const good = '/private/var/folders/ab/c_d-9/T/hermes-owner-anchor-Ab12Cd/manifest.sha256'
    const sha = 'a'.repeat(64)
    expect(buildAdminArgv(good, sha)).toHaveLength(9)

    for (const bad of [
      'relative/hermes-owner-anchor-Ab12Cd/manifest.sha256',
      '/tmp/hermes-owner-anchor-Ab12Cd/../manifest.sha256',
      "/tmp/x'y/hermes-owner-anchor-Ab12Cd/manifest.sha256",
      '/tmp/$(id)/hermes-owner-anchor-Ab12Cd/manifest.sha256',
      '/tmp/a b/hermes-owner-anchor-Ab12Cd/manifest.sha256',
      '/tmp/a\nb/hermes-owner-anchor-Ab12Cd/manifest.sha256',
      '/tmp/a"b/hermes-owner-anchor-Ab12Cd/manifest.sha256',
      '/tmp/hermes-owner-anchor-Ab12Cd/other.json',
      '/tmp/hermes-owner-anchor-Ab12Cd/anchor.json',
      '/Library/Application Support/Hermes/owner-grant/manifest.sha256'
    ]) {
      expect(() => buildAdminArgv(bad, sha), bad).toThrow()
    }

    for (const badSha of ['A'.repeat(64), 'a'.repeat(63), 'a'.repeat(65), 'g'.repeat(64), `${'a'.repeat(63)};`, '']) {
      expect(() => buildAdminArgv(good, badSha), badSha).toThrow()
    }
  })

  test('E-9c the real runner execs /usr/bin/osascript with the argv array (no shell) and maps a cancel', async () => {
    const calls: Array<{ file: string; args: readonly string[]; opts: any }> = []
    let fail: any = null

    const execFile = (file: string, args: readonly string[], opts: any, cb: (err: any, stdout: string, stderr: string) => void) => {
      calls.push({ file, args, opts })
      cb(fail, '', fail ? 'execution error: User canceled. (-128)' : '')
    }

    const runner = createOsascriptAdminRunner({ execFile })
    const argv = buildAdminArgv('/tmp/x/hermes-owner-anchor-Ab12Cd/manifest.sha256', 'b'.repeat(64))
    expect(await runner.run(argv)).toEqual({ code: 0, cancelled: false })
    expect(calls[0].file).toBe('/usr/bin/osascript')
    expect(calls[0].args).toEqual(argv)
    expect(calls[0].opts?.shell).toBeFalsy()
    fail = Object.assign(new Error('Command failed'), { code: 1 })
    expect(await runner.run(argv)).toEqual({ code: 1, cancelled: true })
  })

  test('E-9d the root script installs 0644 in 0755 dirs, and refuses bytes that differ from the hash', async () => {
    const h = harness()
    const res = await h.controller.enable()
    expect(res.ok).toBe(true)
    const top = h.rfs.real(HERMES_TOP)
    const dir = h.rfs.real(OWNER_ANCHOR_DIR)
    expect(fs.statSync(top).mode & 0o777).toBe(0o755)
    expect(fs.statSync(dir).mode & 0o777).toBe(0o755)
    expect(fs.statSync(h.rfs.real(OWNER_ANCHOR_PATH)).mode & 0o777).toBe(0o644)
    expect(leftovers(dir).sort()).toEqual(['anchor.json', 'hermes_owner_verify.py', 'manifest.sha256', 'verifier'])

    // An agent rewrites the staged file after the app hashed it: the root copy fails the hash.
    const swapped = harness({
      before: src => {
        const anchorFile = path.join(path.dirname(src), 'anchor.json')
        fs.writeFileSync(anchorFile, fs.readFileSync(anchorFile, 'utf8').replace('"owner_uid"', '"owner_uid" '))
      },
      ss: mockSafeStorage()
    })
    const bad = await swapped.controller.enable()
    expect(bad.ok).toBe(false)
    expect(fs.existsSync(swapped.rfs.real(OWNER_ANCHOR_PATH))).toBe(false)
    expect(leftovers(swapped.rfs.real(OWNER_ANCHOR_DIR))).toEqual([])
    expect(swapped.controller.status().canSign).toBe(false)
  })

  test.runIf(process.platform === 'darwin')(
    'E-9e osascript hands hostile arguments to sh verbatim (admin clause stripped: no prompt)',
    () => {
      const adminClause = ADMIN_APPLESCRIPT[1].slice(ADMIN_APPLESCRIPT[1].indexOf(' with prompt '))
      expect(adminClause.endsWith(' with administrator privileges')).toBe(true)
      const line = ADMIN_APPLESCRIPT[1].slice(0, ADMIN_APPLESCRIPT[1].length - adminClause.length)
      expect(line).not.toContain('administrator')
      const hostile = `/tmp/a'b"c$(touch /tmp/pwned-u11)\`id\`;x`
      const res = spawnSync(
        OSASCRIPT_PATH,
        ['-e', ADMIN_APPLESCRIPT[0], '-e', line, '-e', ADMIN_APPLESCRIPT[2], 'printf "%s|%s|%s" "$0" "$1" "$2"', hostile, 'x y'],
        { encoding: 'utf8', timeout: 20_000 }
      )
      expect(res.status).toBe(0)
      // osascript prints the result plus one newline.
      expect(res.stdout).toBe(`hermes-owner-anchor|${hostile}|x y\n`)
      expect(fs.existsSync('/tmp/pwned-u11')).toBe(false)
    }
  )
})

// -- install ---------------------------------------------------------------------------------------

describe('U11 install: one admin step pins a fresh key; success only when the anchor now pins it', () => {
  test('U11-I1 enable makes a new key, shows its kid, pins it root-owned, and signing works', async () => {
    const h = harness()
    expect(h.controller.launch().state).toBe('off')
    const res = await h.controller.enable()

    expect(res.ok).toBe(true)
    const kid = h.store.publicInfo()!.kid
    expect(res.kid).toBe(kid)
    expect(h.confirms).toHaveLength(1)
    expect(`${h.confirms[0].message}\n${h.confirms[0].detail}`).toContain(kid)
    expect(h.confirms[0].detail).toMatch(/admin password once/)
    // The Keychain is touched only after the owner confirmed.
    expect(h.confirms[0].keychainCallsAtConfirm).toBe(0)
    // The staged anchor was 0600 in a 0700 dir, and is gone afterwards.
    expect(h.seen[0].srcMode).toBe(0o600)
    expect(h.seen[0].srcDirMode).toBe(0o700)
    expect(leftovers(h.tmpRoot)).toEqual([])

    const doc = anchorOnDisk(h)
    expect(doc).toEqual({
      format: 'hermes-owner-anchor/v1',
      owner_uid: UID,
      grants_dir: '/Users/owner/.hermes/owner-grants',
      keys: [{ kid, alg: 'Ed25519', pub: h.store.publicInfo()!.pub, status: 'active', not_before: NOW, retired_at: null }],
      verifier_sha256: sha256Hex(OWNER_VERIFY_LAUNCHER)
    })
    expect(blobKid(h.keyDir)).toBe(kid)
    expect(leftovers(h.keyDir)).toEqual([OWNER_KEY_FILE])
    expect(h.controller.status()).toMatchObject({ state: 'ready', canSign: true, kid, anchorKid: kid })
    expect(signsWith(h.store, h.store.publicInfo()!.pub)).toBe(true)

    // A restart with the same key dir and anchor loads it again.
    const again = createOwnerKeyStore({ safeStorage: h.ss.api, keyDir: h.keyDir })
    const relaunch = createOwnerGrantController({
      store: again,
      readAnchor: h.readAnchor,
      adminRunner: h.runner,
      confirm: async () => true,
      ownerUid: UID,
      grantsDir: '/Users/owner/.hermes/owner-grants',
      platform: 'darwin'
    })
    expect(relaunch.launch()).toMatchObject({ state: 'ready', canSign: true, kid })
  })

  test('U11-I2 (D20) a blob planted before enable is NOT adopted: a fresh key replaces it', async () => {
    const keyDir = path.join(mkTmp('ogai-home-'), 'owner-grants', '.key')
    const ss = mockSafeStorage()
    const planted = plantBlob(keyDir, ss)
    const h = harness({ keyDir, ss })
    expect(h.controller.launch()).toMatchObject({ state: 'off', canSign: false, kid: null })
    ss.calls.length = 0

    const res = await h.controller.enable()
    expect(res.ok).toBe(true)
    const kid = h.store.publicInfo()!.kid
    expect(kid).not.toBe(planted.kid)
    expect(ss.calls).not.toContain('decryptString')
    expect(`${h.confirms[0].message}\n${h.confirms[0].detail}`).not.toContain(planted.kid)
    expect(anchorOnDisk(h).keys.map((k: any) => k.kid)).toEqual([kid])
    expect(blobKid(keyDir)).toBe(kid)
    expect(signsWith(h.store, h.store.publicInfo()!.pub)).toBe(true)
  })

  test('U11-I3 the runner reports success but the anchor does not pin the new key: reported as failure', async () => {
    // (a) writes nothing at all.
    const silent = harness({ runner: () => ({ run: async () => ({ code: 0 }) }) })
    const a = await silent.controller.enable()
    expect(a).toMatchObject({ ok: false, reason: 'not_pinned' })
    expect(silent.controller.status()).toMatchObject({ state: 'off', canSign: false })
    expect(blobKid(silent.keyDir)).toBeNull()
    expect(leftovers(silent.keyDir)).toEqual([])
    expect(leftovers(silent.tmpRoot)).toEqual([])
    expect(() => silent.store.signEnvelope(Buffer.from('{}'), [])).toThrow()

    // (b) installs a root-owned anchor for SOME OTHER key (a phished or raced install).
    const other = harness()
    await other.controller.enable()
    const otherAnchor = fs.readFileSync(other.rfs.real(OWNER_ANCHOR_PATH))

    const lying = harness({
      runner: rfs => ({
        run: async () => {
          const dir = rfs.real(OWNER_ANCHOR_DIR)
          fs.mkdirSync(dir, { recursive: true, mode: 0o755 })
          fs.chmodSync(rfs.real(HERMES_TOP), 0o755)
          fs.writeFileSync(rfs.real(OWNER_ANCHOR_PATH), otherAnchor, { mode: 0o644 })

          for (const p of [HERMES_TOP, OWNER_ANCHOR_DIR, OWNER_ANCHOR_PATH]) {
            rfs.rootOwned.add(p)
          }

          return { code: 0 }
        }
      })
    })
    const b = await lying.controller.enable()
    expect(b).toMatchObject({ ok: false, reason: 'not_pinned' })
    expect(lying.controller.status()).toMatchObject({ state: 'mismatch', canSign: false })
    expect(blobKid(lying.keyDir)).toBeNull()
    expect(leftovers(lying.keyDir)).toEqual([])
  })

  test('U11-I4 the owner declines the confirm: no Keychain touch, no runner, no files', async () => {
    const h = harness({ confirm: false })
    const res = await h.controller.enable()
    expect(res).toMatchObject({ ok: false, reason: 'cancelled' })
    expect(h.seen).toHaveLength(0)
    expect(h.ss.calls.filter(c => c !== 'isEncryptionAvailable')).toEqual([])
    expect(leftovers(h.keyDir)).toEqual([])
    expect(leftovers(h.tmpRoot)).toEqual([])
  })

  test('U11-I5 the admin prompt is cancelled: nothing is committed and the staged files are gone', async () => {
    const h = harness({ runner: () => ({ run: async () => ({ code: 1, cancelled: true }) }) })
    const res = await h.controller.enable()
    expect(res).toMatchObject({ ok: false, reason: 'cancelled' })
    expect(leftovers(h.keyDir)).toEqual([])
    expect(leftovers(h.tmpRoot)).toEqual([])
    expect(h.controller.status().canSign).toBe(false)
  })

  test('U11-I6 enable when the anchor already pins this key is a no-op (no prompt, no new key)', async () => {
    const h = harness()
    await h.controller.enable()
    const kid = h.store.publicInfo()!.kid
    const again = await h.controller.enable()
    expect(again).toMatchObject({ ok: true, kid, unchanged: true })
    expect(h.seen).toHaveLength(1)
    expect(h.confirms).toHaveLength(1)
  })

  test('U11-I7 only one flow runs at a time', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>(resolve => (release = resolve))
    const h = harness({
      runner: rfs => {
        const inner = fakeAdminRunner(rfs).runner

        return { run: async argv => (await gate, inner.run(argv)) }
      }
    })
    const first = h.controller.enable()
    await new Promise(resolve => setTimeout(resolve, 10))
    expect(await h.controller.rotate()).toMatchObject({ ok: false, reason: 'busy' })
    expect(h.controller.status().busy).toBe(true)
    release()
    expect((await first).ok).toBe(true)
    expect(h.controller.status().busy).toBe(false)
  })

  test('U11-I8 off macOS the flow is unsupported and runs nothing', async () => {
    const ss = mockSafeStorage()
    const store = createOwnerKeyStore({ safeStorage: ss.api, keyDir: path.join(mkTmp('ogai-home-'), 'k') })
    let ran = false
    const controller = createOwnerGrantController({
      store,
      readAnchor: () => ({ ok: false, reason: 'anchor_missing', detail: 'x' }),
      adminRunner: { run: async () => ((ran = true), { code: 0 }) },
      confirm: async () => true,
      ownerUid: UID,
      grantsDir: '/Users/owner/.hermes/owner-grants',
      platform: 'linux'
    })
    expect(controller.status().state).toBe('unsupported')
    expect(await controller.enable()).toMatchObject({ ok: false, reason: 'unsupported' })
    expect(ran).toBe(false)
  })
})

// -- rotate / revoke ---------------------------------------------------------------------------------

describe('U11 rotate and revoke go through the same admin path', () => {
  test('U11-R1 rotate: a fresh key becomes active, the old one retired at now, and the old blob is replaced', async () => {
    let now = NOW
    const h = harness({ now: () => now })
    await h.controller.enable()
    const old = h.store.publicInfo()!
    now = NOW + 60_000
    const res = await h.controller.rotate()

    expect(res.ok).toBe(true)
    const fresh = h.store.publicInfo()!
    expect(fresh.kid).not.toBe(old.kid)
    expect(`${h.confirms[1].message}\n${h.confirms[1].detail}`).toContain(fresh.kid)
    expect(anchorOnDisk(h).keys).toEqual([
      { kid: old.kid, alg: 'Ed25519', pub: old.pub, status: 'retired', not_before: NOW, retired_at: NOW + 60_000 },
      { kid: fresh.kid, alg: 'Ed25519', pub: fresh.pub, status: 'active', not_before: NOW + 60_000, retired_at: null }
    ])
    expect(blobKid(h.keyDir)).toBe(fresh.kid)
    expect(leftovers(h.keyDir)).toEqual([OWNER_KEY_FILE])
    expect(h.controller.status()).toMatchObject({ state: 'ready', canSign: true, kid: fresh.kid })
    expect(signsWith(h.store, fresh.pub)).toBe(true)
  })

  test('U11-R2 rotate over an anchor whose active key this app does not hold revokes that key', async () => {
    const h = harness()
    await h.controller.enable()
    const mine = h.store.publicInfo()!
    // The blob is swapped and the app relaunches: the anchor's active key is not the loaded one.
    const ss = h.ss
    const attackerDir = path.join(mkTmp('ogai-att-'), 'k')
    plantBlob(attackerDir, ss)
    fs.copyFileSync(path.join(attackerDir, OWNER_KEY_FILE), path.join(h.keyDir, OWNER_KEY_FILE))
    const h2 = harness({ rfs: h.rfs, keyDir: h.keyDir, ss })
    expect(h2.controller.launch().state).toBe('mismatch')

    const res = await h2.controller.rotate()
    expect(res.ok).toBe(true)
    const fresh = h2.store.publicInfo()!
    expect(anchorOnDisk(h2).keys.map((k: any) => [k.kid, k.status])).toEqual([
      [mine.kid, 'revoked'],
      [fresh.kid, 'active']
    ])
  })

  test('U11-V1 revoke: the active key becomes revoked, and signing stops at once', async () => {
    const h = harness()
    await h.controller.enable()
    const mine = h.store.publicInfo()!
    const res = await h.controller.revoke()

    expect(res.ok).toBe(true)
    expect(`${h.confirms[1].message}\n${h.confirms[1].detail}`).toContain(mine.kid)
    expect(anchorOnDisk(h).keys).toEqual([
      { kid: mine.kid, alg: 'Ed25519', pub: mine.pub, status: 'revoked', not_before: NOW, retired_at: null }
    ])
    expect(h.controller.status()).toMatchObject({ state: 'revoked', canSign: false })
    expect(() => h.store.signEnvelope(Buffer.from('{}'), [])).toThrowError(expect.objectContaining({ code: 'anchor_mismatch' }))

    // Turning it on again after a revoke pins a fresh key and keeps the revoked one revoked.
    const again = await h.controller.enable()
    expect(again.ok).toBe(true)
    expect(anchorOnDisk(h).keys.map((k: any) => k.status)).toEqual(['revoked', 'active'])
  })

  test('U11-V2 revoke with no trusted anchor, or no active key, runs nothing', async () => {
    const h = harness()
    expect(await h.controller.revoke()).toMatchObject({ ok: false, reason: 'anchor_missing' })
    await h.controller.enable()
    await h.controller.revoke()
    expect(await h.controller.revoke()).toMatchObject({ ok: false, reason: 'no_active_key' })
    expect(h.seen).toHaveLength(2)
  })

  test('U11-V3 a revoke the runner claims but the anchor does not show is a failure', async () => {
    const h = harness()
    await h.controller.enable()
    const kid = h.store.publicInfo()!.kid
    const lie = createOwnerGrantController({
      store: h.store,
      readAnchor: h.readAnchor,
      adminRunner: { run: async () => ({ code: 0 }) },
      confirm: async () => true,
      ownerUid: UID,
      grantsDir: '/Users/owner/.hermes/owner-grants',
      verifierSourceDir: VERIFIER_SOURCE_DIR,
      tmpRoot: h.tmpRoot,
      now: () => NOW,
      platform: 'darwin'
    })
    expect(await lie.revoke()).toMatchObject({ ok: false, reason: 'not_pinned' })
    expect(anchorOnDisk(h).keys[0]).toMatchObject({ kid, status: 'active' })
  })
})

// -- T-3 / T-4: launch refusal ------------------------------------------------------------------------

describe('T-3 / T-4: the launch check refuses to sign instead of re-enrolling', () => {
  test('T-3a a user-owned file at the anchor path is untrusted: the matching blob is not loaded', async () => {
    const keyDir = path.join(mkTmp('ogai-home-'), 'owner-grants', '.key')
    const ss = mockSafeStorage()
    const planted = plantBlob(keyDir, ss)
    const rfs = new RedirectFs(mkTmp('ogai-root-'))
    const dir = rfs.real(OWNER_ANCHOR_DIR)
    fs.mkdirSync(dir, { recursive: true, mode: 0o755 })
    fs.writeFileSync(
      rfs.real(OWNER_ANCHOR_PATH),
      JSON.stringify({
        format: 'hermes-owner-anchor/v1',
        owner_uid: UID,
        grants_dir: '/Users/owner/.hermes/owner-grants',
        keys: [{ kid: planted.kid, alg: 'Ed25519', pub: planted.pub, status: 'active', not_before: 0, retired_at: null }]
      }),
      { mode: 0o644 }
    )
    // Directories are "root-owned"; only the file is the agent's.
    rfs.rootOwned.add(HERMES_TOP)
    rfs.rootOwned.add(OWNER_ANCHOR_DIR)
    ss.calls.length = 0

    const h = harness({ rfs, keyDir, ss })
    const status = h.controller.launch()
    expect(status).toMatchObject({ state: 'untrusted', canSign: false, kid: null, anchorKid: null })
    expect(status.message).toMatch(/not installed or tampered/)
    expect(ss.calls).not.toContain('decryptString')
    expect(() => h.store.signEnvelope(Buffer.from('{}'), [])).toThrow()

    // T-3b re-enabling over it writes a fresh anchor that carries none of the untrusted keys.
    const res = await h.controller.enable()
    expect(res.ok).toBe(true)
    expect(anchorOnDisk(h).keys.map((k: any) => k.kid)).toEqual([h.store.publicInfo()!.kid])
    expect(h.store.publicInfo()!.kid).not.toBe(planted.kid)
  })

  test('T-4a a swapped blob at launch: refused with a clear status, nothing re-enrolled, no signing', async () => {
    const first = harness()
    await first.controller.enable()
    const owner = first.store.publicInfo()!
    const anchorBefore = fs.readFileSync(first.rfs.real(OWNER_ANCHOR_PATH))

    const attackerDir = path.join(mkTmp('ogai-att-'), 'k')
    const attacker = plantBlob(attackerDir, first.ss)
    fs.copyFileSync(path.join(attackerDir, OWNER_KEY_FILE), path.join(first.keyDir, OWNER_KEY_FILE))
    const blobBefore = fs.readFileSync(path.join(first.keyDir, OWNER_KEY_FILE))
    first.ss.calls.length = 0

    const h = harness({ rfs: first.rfs, keyDir: first.keyDir, ss: first.ss })
    const status = h.controller.launch()
    expect(status).toMatchObject({
      state: 'mismatch',
      canSign: false,
      kid: null,
      anchorKid: owner.kid,
      refusal: 'anchor_mismatch'
    })
    expect(status.message).toMatch(/doesn't match this app's key/)
    expect(h.controller.status()).toMatchObject({ state: 'mismatch', refusal: 'anchor_mismatch' })
    // No silent re-enrollment: no Keychain wrap or unwrap, no new blob, no admin step, anchor intact.
    expect(first.ss.calls.filter(c => c === 'encryptString' || c === 'decryptString')).toEqual([])
    expect(fs.readFileSync(path.join(first.keyDir, OWNER_KEY_FILE))).toEqual(blobBefore)
    expect(leftovers(first.keyDir)).toEqual([OWNER_KEY_FILE])
    expect(h.seen).toHaveLength(0)
    expect(h.confirms).toHaveLength(0)
    expect(fs.readFileSync(first.rfs.real(OWNER_ANCHOR_PATH))).toEqual(anchorBefore)
    expect(() => h.store.signEnvelope(Buffer.from('{}'), [])).toThrow()
    expect(attacker.kid).not.toBe(owner.kid)
  })

  test('T-4b an anchor with no blob at all (lost key): refused, and no key is generated at launch', async () => {
    const first = harness()
    await first.controller.enable()
    const owner = first.store.publicInfo()!
    const freshHome = path.join(mkTmp('ogai-home-'), 'owner-grants', '.key')
    const ss = mockSafeStorage()
    const h = harness({ rfs: first.rfs, keyDir: freshHome, ss })
    expect(h.controller.launch()).toMatchObject({ state: 'mismatch', canSign: false, kid: null, anchorKid: owner.kid })
    expect(ss.calls.filter(c => c === 'encryptString' || c === 'decryptString')).toEqual([])
    expect(leftovers(freshHome)).toEqual([])
  })

  test('T-4c launch reads the anchor, sets it, then loads (the anchor gates the blob)', () => {
    const order: string[] = []
    const ss = mockSafeStorage()
    const store = createOwnerKeyStore({ safeStorage: ss.api, keyDir: path.join(mkTmp('ogai-home-'), 'k') })
    const spy = new Proxy(store, {
      get(target, prop, receiver) {
        const value = Reflect.get(target, prop, receiver)

        if (typeof value === 'function' && (prop === 'setAnchor' || prop === 'loadIfEnrolled' || prop === 'ensure')) {
          return (...args: unknown[]) => (order.push(String(prop)), value.apply(target, args))
        }

        return typeof value === 'function' ? value.bind(target) : value
      }
    })
    const controller = createOwnerGrantController({
      store: spy,
      readAnchor: () => (order.push('readAnchor'), { ok: false, reason: 'anchor_missing', detail: 'x' }),
      adminRunner: { run: async () => ({ code: 0 }) },
      confirm: async () => true,
      ownerUid: UID,
      grantsDir: '/Users/owner/.hermes/owner-grants',
      platform: 'darwin'
    })
    controller.launch()
    expect(order).toEqual(['readAnchor', 'setAnchor', 'loadIfEnrolled'])
  })
})

// -- main / preload wiring ---------------------------------------------------------------------------

describe('U11 wiring', () => {
  test('U11-W1 main launches through the controller and exposes status + action over IPC; preload bridges both', () => {
    const main = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8')
    const preload = fs.readFileSync(path.join(__dirname, 'preload.ts'), 'utf8')
    const start = main.indexOf('function loadOwnerGrantKeyAtLaunch')
    expect(start).toBeGreaterThan(0)
    const body = main.slice(start, main.indexOf('\n}\n', start))
    expect(body).toContain('ownerGrantController.launch()')
    expect(main).toContain("ipcMain.handle('hermes:owner-grant:status'")
    expect(main).toContain("ipcMain.handle('hermes:owner-grant:action'")
    expect(main).toContain('readTrustedOwnerAnchor()')
    expect(main).toContain('createOsascriptAdminRunner(')
    expect(main).not.toMatch(/ownerGrantKeyStore\.ensure\(/)
    expect(preload).toContain("ipcRenderer.invoke('hermes:owner-grant:status')")
    expect(preload).toContain("ipcRenderer.invoke('hermes:owner-grant:action'")
  })

  test('U11-W2 the IPC action entry point accepts only the three known actions', async () => {
    const h = harness()
    expect(await h.controller.runAction('format-disk')).toMatchObject({ ok: false, reason: 'bad_action' })
    expect(await h.controller.runAction({ action: 'enable' })).toMatchObject({ ok: false, reason: 'bad_action' })
    expect(h.seen).toHaveLength(0)
    expect((await h.controller.runAction('enable')).ok).toBe(true)
  })
})

// -- cross-language: the Python verifier accepts every anchor this flow writes ------------------------

const PY_CHILD = String.raw`
import json, sys
req = json.load(sys.stdin)
sys.path.insert(0, req["repo"])
from hermes_owner_grant import anchor as A
out = []
for item in req["anchors"]:
    a = A.parse_anchor(item["bytes"].encode("utf-8"))
    row = {"owner_uid": a.owner_uid, "grants_dir": a.grants_dir,
           "keys": [[k.kid, k.status, k.not_before, k.retired_at] for k in a.keys],
           "active": a.active_key.kid if a.active_key else None, "checks": {}}
    for kid, issued_at, now in item["checks"]:
        row["checks"]["%s@%d/%d" % (kid, issued_at, now)] = A.check_key(a, kid, issued_at=issued_at, now=now)
    out.append(row)
json.dump(out, sys.stdout)
`

describe('U11 cross-language: hermes_owner_grant.anchor.parse_anchor accepts the installed bytes', () => {
  test.runIf(HAVE_PYTHON)('U11-X1 install, rotate and revoke anchors parse in Python with the right key semantics', async () => {
    let now = NOW
    const h = harness({ now: () => now })
    await h.controller.enable()
    const k1 = h.store.publicInfo()!.kid
    const installed = fs.readFileSync(h.rfs.real(OWNER_ANCHOR_PATH), 'utf8')
    now = NOW + 1000
    await h.controller.rotate()
    const k2 = h.store.publicInfo()!.kid
    const rotated = fs.readFileSync(h.rfs.real(OWNER_ANCHOR_PATH), 'utf8')
    now = NOW + 2000
    await h.controller.revoke()
    const revoked = fs.readFileSync(h.rfs.real(OWNER_ANCHOR_PATH), 'utf8')

    const res = spawnSync(PYTHON, ['-I', '-c', PY_CHILD], {
      input: JSON.stringify({
        repo: REPO,
        anchors: [
          { bytes: installed, checks: [[k1, NOW + 5, NOW + 10]] },
          {
            bytes: rotated,
            checks: [
              [k1, NOW + 500, NOW + 2000],
              [k1, NOW + 1500, NOW + 2000],
              [k2, NOW + 1500, NOW + 2000]
            ]
          },
          { bytes: revoked, checks: [[k2, NOW + 1500, NOW + 3000]] }
        ]
      }),
      encoding: 'utf8',
      env: { PATH: '/usr/bin:/bin' }
    })
    expect(res.stderr).toBe('')
    expect(res.status).toBe(0)
    const [a, b, c] = JSON.parse(res.stdout)
    expect(a).toMatchObject({ owner_uid: UID, grants_dir: '/Users/owner/.hermes/owner-grants', active: k1 })
    expect(a.keys).toEqual([[k1, 'active', NOW, null]])
    expect(a.checks[`${k1}@${NOW + 5}/${NOW + 10}`]).toBeNull()
    expect(b.active).toBe(k2)
    expect(b.keys).toEqual([
      [k1, 'retired', NOW, NOW + 1000],
      [k2, 'active', NOW + 1000, null]
    ])
    expect(b.checks[`${k1}@${NOW + 500}/${NOW + 2000}`]).toBeNull()
    expect(b.checks[`${k1}@${NOW + 1500}/${NOW + 2000}`]).toBe('key_retired')
    expect(b.checks[`${k2}@${NOW + 1500}/${NOW + 2000}`]).toBeNull()
    expect(c.active).toBeNull()
    expect(c.checks[`${k2}@${NOW + 1500}/${NOW + 3000}`]).toBe('key_revoked')
  })

  test.skipIf(HAVE_PYTHON)('U11-X1 SKIPPED: no Python for the cross-language check', () => {
    console.warn(`U11-X1 SKIPPED: no Python at ${PYTHON} or no hermes_owner_grant at ${REPO}`)
  })
})

// -- U11b: the same admin step co-installs the root-owned verifier ---------------------------------

const PKG_REL = 'verifier/hermes_owner_grant'

function installedTree(h: Harness): string[] {
  return walk(h.rfs.real(OWNER_ANCHOR_DIR))
}

function installedFile(h: Harness, rel: string): Buffer {
  return fs.readFileSync(path.join(h.rfs.real(OWNER_ANCHOR_DIR), rel))
}

function snapshot(h: Harness): Record<string, string> {
  const out: Record<string, string> = {}

  for (const rel of installedTree(h)) {
    out[rel] = rel.endsWith('/') ? 'dir' : sha256Hex(installedFile(h, rel))
  }

  return out
}

describe('U11b: one admin prompt installs the anchor AND the root-owned verifier (launcher + precompiled package)', () => {
  test('U11b-S1 the staged payload is exactly anchor.json, the launcher, the verifier files and the manifest', async () => {
    const h = harness()
    const res = await h.controller.enable()
    expect(res.ok).toBe(true)
    const seen = h.seen[0]
    const pkgFiles = VERIFIER_PACKAGE_FILES.map(name => `${PKG_REL}/${name}`)

    expect(STAGED_PAYLOAD_FILES).toEqual(['anchor.json', 'hermes_owner_verify.py', ...pkgFiles])
    expect(Object.keys(seen.staged).sort()).toEqual(
      [...STAGED_PAYLOAD_FILES, 'manifest.sha256', 'verifier/', `${PKG_REL}/`].sort()
    )
    // builder.py and doctor.py are tooling and never staged.
    expect(VERIFIER_PACKAGE_FILES).not.toContain('builder.py')
    expect(VERIFIER_PACKAGE_FILES).not.toContain('doctor.py')

    // Private staging: files 0600, dirs 0700.
    for (const [rel, entry] of Object.entries(seen.staged)) {
      expect(entry.mode, rel).toBe(rel.endsWith('/') ? 0o700 : 0o600)
    }

    // The manifest pins every other staged file, in the fixed order, and argv pins the manifest.
    const manifest = seen.staged['manifest.sha256'].content!
    expect(seen.argv[8]).toBe(sha256Hex(manifest))
    expect(manifest.toString('ascii')).toBe(
      STAGED_PAYLOAD_FILES.map(rel => `${sha256Hex(seen.staged[rel].content!)}  ${rel}\n`).join('')
    )
    expect(seen.staged['hermes_owner_verify.py'].content!.toString('utf8')).toBe(OWNER_VERIFY_LAUNCHER)

    for (const name of VERIFIER_PACKAGE_FILES) {
      expect(seen.staged[`${PKG_REL}/${name}`].content).toEqual(fs.readFileSync(path.join(VERIFIER_SOURCE_DIR, name)))
    }

    // The root script checks every file: it names each one and compares against the manifest.
    for (const rel of STAGED_PAYLOAD_FILES) {
      expect(ANCHOR_ROOT_SCRIPT).toContain(rel.replace(PKG_REL, '$pkg'))
    }

    expect(ANCHOR_ROOT_SCRIPT).toContain('/usr/bin/cmp -s')
    expect(leftovers(h.tmpRoot)).toEqual([])
  })

  test('U11b-S2 installed root-owned: dirs 0755, files 0644, precompiled, manifest written, anchor pins the launcher', async () => {
    const h = harness({ warm: true })
    expect((await h.controller.enable()).ok).toBe(true)
    const tree = installedTree(h)
    const pyFiles = VERIFIER_PACKAGE_FILES.filter(n => n.endsWith('.py'))

    // Nothing writable by group or other anywhere from the Hermes dir down.
    for (const rel of ['', ...tree]) {
      const st = fs.lstatSync(path.join(h.rfs.real(OWNER_ANCHOR_DIR), rel))
      expect(st.isSymbolicLink(), rel).toBe(false)
      expect(st.mode & 0o777, rel).toBe(st.isDirectory() ? 0o755 : 0o644)
    }

    expect(fs.statSync(h.rfs.real(HERMES_TOP)).mode & 0o777).toBe(0o755)
    expect(tree.filter(r => !r.startsWith('pycache/'))).toEqual([
      'anchor.json',
      'hermes_owner_verify.py',
      'manifest.sha256',
      'verifier/',
      `${PKG_REL}/`,
      ...VERIFIER_PACKAGE_FILES.map(n => `${PKG_REL}/${n}`).sort()
    ])
    const pycs = tree.filter(r => r.endsWith('.pyc'))

    // pycache/ is the launcher's sys.pycache_prefix: root compiled the package AND the stdlib it
    // imports (the Command Line Tools stdlib ships no __pycache__ of its own).
    if (HAVE_SYSTEM_PYTHON) {
      expect(tree).toContain('pycache/')

      for (const name of pyFiles) {
        expect(
          pycs.some(r => r.endsWith(`/${PKG_REL}/${name.slice(0, -3)}.cpython-39.pyc`)),
          name
        ).toBe(true)
      }

      for (const mod of ['argparse', 'dataclasses', 'inspect', 'typing', 'json/decoder']) {
        expect(
          pycs.some(r => r.endsWith(`/${mod}.cpython-39.pyc`)),
          mod
        ).toBe(true)
      }
    }

    // The installed manifest (root-written): the launcher, the package and every .pyc.
    const lines = installedFile(h, 'manifest.sha256').toString('ascii').trimEnd().split('\n')
    expect(lines.map(l => l.split('  ')[1])).toEqual([
      'hermes_owner_verify.py',
      ...VERIFIER_PACKAGE_FILES.map(n => `${PKG_REL}/${n}`),
      ...pycs
    ])

    for (const line of lines) {
      const [digest, rel] = line.split('  ')
      expect(digest, rel).toBe(sha256Hex(installedFile(h, rel)))
    }

    const launcherSha = sha256Hex(installedFile(h, 'hermes_owner_verify.py'))
    expect(launcherSha).toBe(sha256Hex(OWNER_VERIFY_LAUNCHER))
    expect(anchorOnDisk(h).verifier_sha256).toBe(launcherSha)
    const read = h.readAnchor()
    expect(read.ok && read.anchor.verifierSha256).toBe(launcherSha)
    expect(h.controller.status()).toMatchObject({ state: 'ready', canSign: true })
  })

  test('U11b-S3 a verifier file tampered between staging and install fails the install and leaves nothing', async () => {
    const h = harness({
      before: src => {
        const target = path.join(path.dirname(src), PKG_REL, 'verify.py')
        fs.appendFileSync(target, '\nimport os; os.system("id")\n')
      }
    })
    const res = await h.controller.enable()
    expect(res).toMatchObject({ ok: false, reason: 'admin_failed' })
    expect(leftovers(h.rfs.real(OWNER_ANCHOR_DIR))).toEqual([])
    expect(fs.existsSync(h.rfs.real(OWNER_ANCHOR_PATH))).toBe(false)
    expect(h.controller.status()).toMatchObject({ canSign: false, anchorKid: null })
    expect(blobKid(h.keyDir)).toBeNull()
    expect(leftovers(h.tmpRoot)).toEqual([])

    // The launcher and the anchor are pinned the same way.
    for (const rel of ['hermes_owner_verify.py', 'anchor.json']) {
      const t = harness({ before: src => fs.appendFileSync(path.join(path.dirname(src), rel), ' ') })
      expect((await t.controller.enable()).ok, rel).toBe(false)
      expect(leftovers(t.rfs.real(OWNER_ANCHOR_DIR)), rel).toEqual([])
    }
  })

  test('U11b-S4 rewriting a file AND its manifest line consistently still fails (argv pins the manifest)', async () => {
    const h = harness({
      before: src => {
        const dir = path.dirname(src)
        const target = path.join(dir, PKG_REL, 'cli.py')
        const oldSha = sha256Hex(fs.readFileSync(target))
        fs.appendFileSync(target, '\n# agent\n')
        const newSha = sha256Hex(fs.readFileSync(target))
        fs.writeFileSync(src, fs.readFileSync(src, 'ascii').replace(oldSha, newSha))
      }
    })
    expect((await h.controller.enable()).ok).toBe(false)
    expect(leftovers(h.rfs.real(OWNER_ANCHOR_DIR))).toEqual([])
  })

  test('U11b-S5 a staged file swapped for a symlink is refused', async () => {
    const outside = path.join(mkTmp('ogai-out-'), 'verify.py')
    fs.copyFileSync(path.join(VERIFIER_SOURCE_DIR, 'verify.py'), outside)
    const h = harness({
      before: src => {
        const target = path.join(path.dirname(src), PKG_REL, 'verify.py')
        fs.rmSync(target)
        fs.symlinkSync(outside, target)
      }
    })
    expect((await h.controller.enable()).ok).toBe(false)
    expect(leftovers(h.rfs.real(OWNER_ANCHOR_DIR))).toEqual([])
  })

  test('U11b-S6 a failed rotate leaves the previous anchor, launcher and verifier exactly as they were', async () => {
    let tamper = false
    const h = harness({
      before: src => {
        if (tamper) {
          fs.appendFileSync(path.join(path.dirname(src), PKG_REL, 'anchor.py'), '\n# agent\n')
        }
      }
    })
    expect((await h.controller.enable()).ok).toBe(true)
    const kid = h.store.publicInfo()!.kid
    const before = snapshot(h)
    tamper = true
    const res = await h.controller.rotate()
    expect(res.ok).toBe(false)
    expect(snapshot(h)).toEqual(before)
    expect(h.controller.status()).toMatchObject({ state: 'ready', canSign: true, kid })
  })

  test('U11b-S8 enable over an anchor that pins our key but not this verifier: same keys, verifier updated', async () => {
    const h = harness()
    expect((await h.controller.enable()).ok).toBe(true)
    const kid = h.store.publicInfo()!.kid
    // An install from before U11b: same key, no verifier_sha256.
    const legacy = anchorOnDisk(h)
    delete legacy.verifier_sha256
    fs.writeFileSync(h.rfs.real(OWNER_ANCHOR_PATH), JSON.stringify(legacy))
    expect(h.controller.status()).toMatchObject({ state: 'ready', kid })

    const res = await h.controller.enable()
    expect(res).toMatchObject({ ok: true, kid })
    expect(res.unchanged).toBeUndefined()
    expect(h.confirms.at(-1)!.title).toBe('Update the owner-grant verifier')
    expect(h.seen).toHaveLength(2)
    expect(anchorOnDisk(h)).toEqual({ ...legacy, verifier_sha256: sha256Hex(OWNER_VERIFY_LAUNCHER) })
    expect(h.store.publicInfo()!.kid).toBe(kid)

    // Current verifier: nothing to do, no prompt.
    expect(await h.controller.enable()).toMatchObject({ ok: true, kid, unchanged: true })
    expect(h.seen).toHaveLength(2)
  })

  test('U11b-S7 no verifier sources: refused before any confirm, key or admin prompt', async () => {
    const h = harness({ verifierSourceDir: path.join(mkTmp('ogai-nosrc-'), 'hermes_owner_grant') })
    const res = await h.controller.enable()
    expect(res).toMatchObject({ ok: false, reason: 'verifier_unavailable' })
    expect(h.confirms).toHaveLength(0)
    expect(h.seen).toHaveLength(0)
    expect(h.ss.calls.filter(c => c !== 'isEncryptionAvailable')).toEqual([])
    expect(blobKid(h.keyDir)).toBeNull()
  })
})

const PY_INSTALL_CHILD = String.raw`
import json, os, sys
from types import SimpleNamespace
req = json.load(sys.stdin)
sys.path.insert(0, req["repo"])
from hermes_owner_grant import anchor as A, builder, install_check

class RootFs:
    def __init__(self):
        self.real = A.OsFileSystem()
    def _fake(self, st):
        return SimpleNamespace(st_mode=st.st_mode & ~0o022, st_uid=0, st_dev=st.st_dev,
                               st_ino=st.st_ino, st_size=st.st_size)
    def lstat(self, path):
        return self._fake(os.lstat(path))
    def read_nofollow(self, path, limit):
        st, data = self.real.read_nofollow(path, limit)
        return self._fake(st), data
    def listdir(self, path):
        return os.listdir(path)

anchor = A.parse_anchor(open(os.path.join(req["dir"], "anchor.json"), "rb").read())
out = {"launcher": builder.LAUNCHER_SOURCE, "package": list(builder.PACKAGE_FILES),
       "warm": builder.WARM_PROGRAM, "checks": []}
for tamper in req["tamper"]:
    if tamper:
        with open(os.path.join(req["dir"], tamper), "ab") as f:
            f.write(b"\n# drift\n")
    out["checks"].append(install_check.check_installed_verifier(anchor, install_dir=req["dir"], fs=RootFs()))
json.dump(out, sys.stdout)
`

describe('U11b cross-language: the Python doctor check reads what the root script installed', () => {
  test.runIf(HAVE_PYTHON)('U11b-X1 same launcher and file list; match on the real install; mismatch after drift', async () => {
    const h = harness({ warm: true })
    expect((await h.controller.enable()).ok).toBe(true)
    const res = spawnSync(PYTHON, ['-I', '-c', PY_INSTALL_CHILD], {
      // realpath: /var is a symlink on macOS, and the StrictModes chain check refuses it.
      input: JSON.stringify({ repo: REPO, dir: fs.realpathSync(h.rfs.real(OWNER_ANCHOR_DIR)), tamper: ['', `${PKG_REL}/quote.py`] }),
      encoding: 'utf8',
      env: { PATH: '/usr/bin:/bin' }
    })
    expect(res.stderr).toBe('')
    expect(res.status).toBe(0)
    const out = JSON.parse(res.stdout)
    expect(out.launcher).toBe(OWNER_VERIFY_LAUNCHER)
    expect(out.package).toEqual([...VERIFIER_PACKAGE_FILES])
    expect(ANCHOR_ROOT_SCRIPT).toContain(`-c '${out.warm}' "$dir/verifier"`)
    expect(out.checks[0], JSON.stringify(out.checks[0])).toMatchObject({ state: "match", mismatched: [], precompiled: HAVE_SYSTEM_PYTHON })
    expect(out.checks[1]).toMatchObject({ state: 'mismatch', mismatched: [`${PKG_REL}/quote.py`] })
    expect(OWNER_VERIFY_PATH).toBe('/Library/Application Support/Hermes/owner-grant/hermes_owner_verify.py')
  })

  test.skipIf(HAVE_PYTHON)('U11b-X1 SKIPPED: no Python for the cross-language check', () => {
    console.warn(`U11b-X1 SKIPPED: no Python at ${PYTHON} or no hermes_owner_grant at ${REPO}`)
  })
})

describe('b9 §6: enable re-pushes the verifier PACKAGE when the installed manifest differs', () => {
  function packageCopy(): string {
    const dir = mkTmp('ogai-pkg-')

    for (const name of VERIFIER_PACKAGE_FILES) {
      fs.copyFileSync(path.join(VERIFIER_SOURCE_DIR, name), path.join(dir, name))
    }

    return dir
  }

  const installed = (h: Harness, name: string) => fs.readFileSync(h.rfs.real(`${OWNER_ANCHOR_DIR}/${PKG_REL}/${name}`))

  test('same launcher, same package: a no-op (no prompt)', async () => {
    const h = harness({ manifestReader: true, verifierSourceDir: packageCopy() })
    expect((await h.controller.enable()).ok).toBe(true)
    const kid = h.store.publicInfo()!.kid
    expect(await h.controller.enable()).toMatchObject({ ok: true, kid, unchanged: true })
    expect(h.seen).toHaveLength(1)
    expect(h.confirms).toHaveLength(1)
  })

  test('same launcher, changed scopes.json: one confirm, one admin prompt, same key, new package installed', async () => {
    const src = packageCopy()
    const h = harness({ manifestReader: true, verifierSourceDir: src })
    expect((await h.controller.enable()).ok).toBe(true)
    const kid = h.store.publicInfo()!.kid
    const anchorBefore = fs.readFileSync(h.rfs.real(OWNER_ANCHOR_PATH))

    fs.appendFileSync(path.join(src, 'scopes.json'), '\n')
    const updated = fs.readFileSync(path.join(src, 'scopes.json'))
    expect(installed(h, 'scopes.json')).not.toEqual(updated)

    const res = await h.controller.enable()
    expect(res).toMatchObject({ ok: true, kid })
    expect(res.unchanged).toBeUndefined()
    expect(h.seen).toHaveLength(2)
    expect(h.confirms).toHaveLength(2)
    expect(h.confirms[1].title).toBe('Update the owner-grant verifier')
    expect(installed(h, 'scopes.json')).toEqual(updated)
    // Same keys, same launcher: anchor.json is unchanged and signing stays on.
    expect(fs.readFileSync(h.rfs.real(OWNER_ANCHOR_PATH))).toEqual(anchorBefore)
    expect(h.controller.status().canSign).toBe(true)
    // Settled: a third enable is a no-op again.
    expect(await h.controller.enable()).toMatchObject({ ok: true, unchanged: true })
    expect(h.seen).toHaveLength(2)
  })

  test('a package refresh the admin step did not land is reported as a failure', async () => {
    const src = packageCopy()
    let calls = 0
    const h = harness({
      manifestReader: true,
      verifierSourceDir: src,
      runner: rfs => {
        const inner = fakeAdminRunner(rfs).runner

        return { run: async argv => (++calls === 1 ? inner.run(argv) : { code: 1 }) }
      }
    })
    expect((await h.controller.enable()).ok).toBe(true)
    fs.appendFileSync(path.join(src, 'verify.py'), '\n')

    expect(await h.controller.enable()).toMatchObject({ ok: false, reason: 'admin_failed' })
    expect(calls).toBe(2)
  })

  test('installedVerifierIsCurrent: launcher and every package file must be listed with this app\'s hash', () => {
    const files = new Map(VERIFIER_PACKAGE_FILES.map(n => [`${PKG_REL}/${n}`, fs.readFileSync(path.join(VERIFIER_SOURCE_DIR, n))]))
    const lines = [
      `${sha256Hex(OWNER_VERIFY_LAUNCHER)}  hermes_owner_verify.py`,
      ...[...files].map(([rel, bytes]) => `${sha256Hex(bytes)}  ${rel}`),
      `${'0'.repeat(64)}  pycache/x.pyc`
    ]
    expect(installedVerifierIsCurrent(lines.join('\n') + '\n', files)).toBe(true)
    expect(installedVerifierIsCurrent(null, files)).toBe(false)
    expect(installedVerifierIsCurrent(lines.slice(1).join('\n'), files)).toBe(false)
    expect(installedVerifierIsCurrent(lines.filter(l => !l.endsWith('/scopes.json')).join('\n'), files)).toBe(false)
    expect(installedVerifierIsCurrent(lines.map(l => (l.endsWith('/verify.py') ? `${'f'.repeat(64)}${l.slice(64)}` : l)).join('\n'), files)).toBe(false)
  })
})
