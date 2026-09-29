/**
 * Install, rotate and revoke the root-owned owner-grant anchor, and the launch check that refuses
 * to sign on a mismatch (#60 U11; VERIFY addendum §1.4, §1.5; E-9, T-3, T-4; decision D20).
 *
 * The privileged step is ONE macOS admin prompt:
 *
 *   /usr/bin/osascript -e 'on run argv' -e '<ADMIN_APPLESCRIPT[1]>' -e 'end run' \
 *     <ANCHOR_ROOT_SCRIPT> <staged manifest.sha256> <sha256 of the manifest bytes>
 *
 * Every element of that argv is a constant in this file except the last two, and those are
 * validated (a mkdtemp path of a fixed shape, 64 lowercase hex) before anything runs. osascript
 * is exec'd directly (no shell). The AppleScript is a constant that hands its three arguments to
 * `/bin/sh -c` through `quoted form of`, so nothing is ever interpolated into a command line.
 *
 * U11b: the same prompt co-installs the root-owned verifier. The app stages, in a 0700 mkdtemp
 * with 0600 files, exactly `STAGED_PAYLOAD_FILES` (anchor.json, the launcher
 * `hermes_owner_verify.py`, the `verifier/hermes_owner_grant/` package files) plus
 * `manifest.sha256`, which lists the sha256 of each in a fixed order; argv pins the manifest. The
 * root script copies the fixed file list (names are constants in the script, never read from the
 * payload) into a root-only temp inside the anchor directory, checks the manifest's sha256 and
 * then that the root copies hash to exactly the manifest (so rewriting any staged file after
 * hashing fails closed and installs nothing). Only then does it swap in the package, fill the
 * root-owned `pycache/` (/usr/bin/python3 -I -S under `-X pycache_prefix` finds the CLI's static
 * import closure with modulefinder and py_compiles it, so the package AND the stdlib it imports
 * are precompiled (D13) without ever executing the staged code as root), move the
 * launcher, write the installed `manifest.sha256` (launcher, package, every .pyc) and finally
 * rename anchor.json, whose `verifier_sha256` is the launcher's sha256. Everything is
 * root:wheel, dirs 0755, files 0644. After the runner returns, main re-reads the anchor
 * through `readTrustedOwnerAnchor` (the StrictModes reader) and reports success only when it now
 * says exactly what was asked for, including that it pins the new key. The runner's exit code is
 * never trusted for success.
 *
 * D20: enable and rotate ALWAYS make a fresh key (`OwnerKeyStore.beginFreshKey`). An on-disk blob
 * is never adopted when no anchor pins it, so a blob planted before enable is simply replaced.
 * The fresh key touches the Keychain only after the owner confirmed the native dialog that shows
 * its kid, and it replaces the on-disk blob only after the anchor pins it.
 *
 * Launch (T-3/T-4): read the anchor, set it on the store, then load the blob only if the anchor
 * pins it. On a mismatch nothing is generated, wrapped, written or re-enrolled; the status says
 * why and signing stays off until the owner runs the admin step again.
 *
 * Residual R7 (addendum §1.4): an agent can run the same admin command with its own key; the
 * owner then sees an admin prompt they didn't ask for. The launch check turns a mistaken approval
 * into a loud mismatch instead of a silent takeover.
 */

import childProcess from 'node:child_process'
import { createHash } from 'node:crypto'
import nodeFs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { OWNER_ANCHOR_DIR, OWNER_ANCHOR_FORMAT, type OwnerAnchor, type OwnerAnchorKey, type OwnerAnchorRead } from './owner-grant-anchor'
import { OwnerKeyError, type OwnerKeyLog, type OwnerKeyPublic, type OwnerKeyStore } from './owner-grant-key'

export const OSASCRIPT_PATH = '/usr/bin/osascript'
const MAX_ANCHOR_KEYS = 32

/** The package files co-installed under `verifier/hermes_owner_grant/`, in manifest order. Must
 *  equal `hermes_owner_grant.install_check.PACKAGE_FILES` (a cross-language test pins it). */
export const VERIFIER_PACKAGE_FILES: readonly string[] = Object.freeze([
  '__init__.py',
  'anchor.py',
  'cli.py',
  'ed25519_pure.py',
  'envelope.py',
  'install_check.py',
  'quote.py',
  'scopes.json',
  'scopes.py',
  'verify.py'
])

const VERIFIER_PACKAGE_REL = 'verifier/hermes_owner_grant'
const MAX_VERIFIER_FILE_BYTES = 1024 * 1024

/** Every staged file the manifest pins, in manifest order (`manifest.sha256` itself is pinned
 *  by argv). */
export const STAGED_PAYLOAD_FILES: readonly string[] = Object.freeze([
  'anchor.json',
  'hermes_owner_verify.py',
  ...VERIFIER_PACKAGE_FILES.map(name => `${VERIFIER_PACKAGE_REL}/${name}`)
])

/** The root-owned launcher that `/usr/bin/python3 -I -S` runs on the hook path. Byte-identical to
 *  `hermes_owner_grant.builder.LAUNCHER_SOURCE` (pinned by a cross-language test). */
export const OWNER_VERIFY_LAUNCHER = String.raw`"""Hermes owner-grant verifier launcher. Installed root-owned; do not edit.

Run: /usr/bin/python3 -I -S "/Library/Application Support/Hermes/owner-grant/hermes_owner_verify.py" verify ...
It imports the verifier only from the root-owned verifier/ directory beside this file, and reads
bytecode only from the root-owned pycache/ beside it (the installer filled it, stdlib included).
It reads no env var and never imports from the working directory; without -I -S it
refuses to run (exit 3).
"""

import sys


def _load():
    if not (sys.flags.isolated and sys.flags.no_site):
        return None, "run it as /usr/bin/python3 -I -S <launcher>"
    here = __file__.rpartition("/")[0]
    if not here.startswith("/"):
        return None, "the launcher path is not absolute"
    root = here + "/verifier"
    sys.pycache_prefix = here + "/pycache"
    if sys.path[:1] != [root]:
        sys.path.insert(0, root)
    from hermes_owner_grant import cli
    if not cli.__file__.startswith(root + "/hermes_owner_grant/"):
        return None, "the verifier was not imported from " + root
    return cli, None


def _main():
    cli, why = _load()
    if cli is None:
        import json
        body = {"schema": "hermes-owner-verify/v1", "ok": False, "reason": "verifier_untrusted", "detail": why}
        sys.stdout.write(json.dumps(body, sort_keys=True) + "\n")
        return 3
    return cli.main()


if __name__ == "__main__":
    raise SystemExit(_main())
`

/** `anchor.verifier_sha256` for everything this app installs. */
export const OWNER_VERIFY_LAUNCHER_SHA256 = createHash('sha256').update(OWNER_VERIFY_LAUNCHER, 'utf8').digest('hex')

/** The AppleScript program, one `-e` per line. Its arguments are (1) the root script, (2) the
 *  staged file, (3) the sha256; each reaches sh as one word via `quoted form of`. */
export const ADMIN_APPLESCRIPT: readonly [string, string, string] = Object.freeze([
  'on run argv',
  'do shell script "/bin/sh -c " & quoted form of (item 1 of argv) & " hermes-owner-anchor " & quoted form of (item 2 of argv) & " " & quoted form of (item 3 of argv) with prompt "Hermes wants to pin your owner key and install its verifier so conductor can verify owner decisions." with administrator privileges',
  'end run'
]) as readonly [string, string, string]

/** Runs as root: `sh -c ANCHOR_ROOT_SCRIPT hermes-owner-anchor <staged manifest> <sha256>`.
 *  Exit 64 bad arguments, 65 a hash differs, 66 a staged path is missing or a symlink, 73 a
 *  Hermes directory is a symlink. Nothing is installed unless every hash matched. */
export const ANCHOR_ROOT_SCRIPT = `set -euf
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077
manifest=$1
want=$2
top='/Library/Application Support/Hermes'
dir="$top/owner-grant"
pkg=verifier/hermes_owner_grant
code="$pkg/__init__.py $pkg/anchor.py $pkg/cli.py $pkg/ed25519_pure.py $pkg/envelope.py $pkg/install_check.py $pkg/quote.py $pkg/scopes.json $pkg/scopes.py $pkg/verify.py"
files="anchor.json hermes_owner_verify.py $code"
case $want in
  *[!0-9a-f]*) exit 64 ;;
esac
[ "\${#want}" -eq 64 ] || exit 64
src=\${manifest%/manifest.sha256}
[ "$src/manifest.sha256" = "$manifest" ] || exit 64
for d in "$src" "$src/verifier" "$src/$pkg"; do
  [ -d "$d" ] && [ ! -L "$d" ] || exit 66
done
for d in "$top" "$dir"; do
  [ ! -L "$d" ] || exit 73
  [ -d "$d" ] || /bin/mkdir "$d"
  /usr/sbin/chown root:wheel "$d"
  /bin/chmod 0755 "$d"
done
tmp="$dir/.install.$$"
old="$dir/.old.$$"
trap '/bin/rm -rf "$tmp" "$old"' EXIT
/bin/rm -rf "$tmp" "$old"
/bin/mkdir -p "$tmp/$pkg" "$old"
for f in manifest.sha256 $files; do
  [ -f "$src/$f" ] && [ ! -L "$src/$f" ] || exit 66
  /usr/bin/head -c 1048576 "$src/$f" > "$tmp/$f"
done
got=$(/usr/bin/shasum -a 256 "$tmp/manifest.sha256")
got=\${got%% *}
[ "$got" = "$want" ] || exit 65
(cd "$tmp" && /usr/bin/shasum -a 256 $files) > "$tmp/.computed"
/usr/bin/cmp -s "$tmp/.computed" "$tmp/manifest.sha256" || exit 65
/bin/rm -f "$tmp/.computed" "$tmp/manifest.sha256"
/usr/sbin/chown -R root:wheel "$tmp"
/usr/bin/find "$tmp" -type d -exec /bin/chmod 0755 {} +
/usr/bin/find "$tmp" -type f -exec /bin/chmod 0644 {} +
[ ! -e "$dir/verifier" ] || /bin/mv -f "$dir/verifier" "$old/verifier"
/bin/mv "$tmp/verifier" "$dir/verifier"
[ ! -e "$dir/pycache" ] || /bin/mv -f "$dir/pycache" "$old/pycache"
if /usr/bin/xcode-select -p >/dev/null 2>&1; then
  /usr/bin/python3 -I -S -X pycache_prefix="$tmp/pycache" -c 'import sys, modulefinder, py_compile
finder = modulefinder.ModuleFinder(path=[sys.argv[1]] + sys.path)
finder.import_hook("hermes_owner_grant.cli")
for module in finder.modules.values():
    if (module.__file__ or "").endswith(".py"):
        py_compile.compile(module.__file__)
' "$dir/verifier" >/dev/null 2>&1 || /bin/rm -rf "$tmp/pycache"
fi
if [ -d "$tmp/pycache" ]; then
  /usr/sbin/chown -R root:wheel "$tmp/pycache"
  /usr/bin/find "$tmp/pycache" -type d -exec /bin/chmod 0755 {} +
  /usr/bin/find "$tmp/pycache" -type f -exec /bin/chmod 0644 {} +
  /bin/mv "$tmp/pycache" "$dir/pycache"
fi
/bin/mv -f "$tmp/hermes_owner_verify.py" "$dir/hermes_owner_verify.py"
(
  cd "$dir"
  /usr/bin/shasum -a 256 hermes_owner_verify.py $code
  if [ -d pycache ]; then
    /usr/bin/find pycache -type f -name '*.pyc' -print0 | LC_ALL=C /usr/bin/sort -z | /usr/bin/xargs -0 /usr/bin/shasum -a 256
  fi
) > "$tmp/manifest.sha256"
/bin/chmod 0644 "$tmp/manifest.sha256"
/bin/mv -f "$tmp/manifest.sha256" "$dir/manifest.sha256"
/bin/mv -f "$tmp/anchor.json" "$dir/anchor.json"
`

const STAGED_FILE_RE = /^\/(?:[A-Za-z0-9._+-]+\/)*hermes-owner-anchor-[A-Za-z0-9]{6}\/manifest\.sha256$/
const SHA256_RE = /^[0-9a-f]{64}$/

export class AnchorInstallError extends Error {
  readonly code = 'bad_argument'
}

/** The full osascript argv. Throws unless the two variable parts (the staged manifest path and
 *  its sha256) have their fixed shapes. */
export function buildAdminArgv(stagedFile: string, sha256: string): string[] {
  if (
    typeof stagedFile !== 'string' ||
    !STAGED_FILE_RE.test(stagedFile) ||
    path.posix.normalize(stagedFile) !== stagedFile ||
    stagedFile.split('/').some(part => part === '.' || part === '..')
  ) {
    throw new AnchorInstallError('the staged manifest path has an unexpected shape')
  }

  if (typeof sha256 !== 'string' || !SHA256_RE.test(sha256)) {
    throw new AnchorInstallError('the manifest digest is not 64 lowercase hex')
  }

  return ['-e', ADMIN_APPLESCRIPT[0], '-e', ADMIN_APPLESCRIPT[1], '-e', ADMIN_APPLESCRIPT[2], ANCHOR_ROOT_SCRIPT, stagedFile, sha256]
}

// -- the admin runner (injectable) -----------------------------------------------------------------

export interface AdminRunResult {
  code: number
  cancelled?: boolean
}

/** Runs the privileged command. Production: osascript. Tests: a fake that never prompts. */
export interface AdminRunner {
  run(argv: readonly string[]): Promise<AdminRunResult>
}

type ExecFileLike = (
  file: string,
  args: readonly string[],
  options: { timeout: number; windowsHide: boolean },
  callback: (error: any, stdout: string, stderr: string) => void
) => unknown

export function createOsascriptAdminRunner(opts: { execFile?: ExecFileLike; timeoutMs?: number } = {}): AdminRunner {
  const execFile = opts.execFile ?? (childProcess.execFile as unknown as ExecFileLike)
  const timeout = opts.timeoutMs ?? 5 * 60_000

  return {
    run: argv =>
      new Promise(resolve => {
        execFile(OSASCRIPT_PATH, [...argv], { timeout, windowsHide: true }, (error, _stdout, stderr) => {
          if (!error) {
            resolve({ code: 0, cancelled: false })

            return
          }

          // osascript reports the owner dismissing the admin sheet as error -128.
          const cancelled = /\(-128\)|User cancel+ed/i.test(String(stderr ?? ''))
          resolve({ code: typeof error.code === 'number' && error.code !== 0 ? error.code : 1, cancelled })
        })
      })
  }
}

// -- the controller -------------------------------------------------------------------------------

export type OwnerGrantState = 'off' | 'ready' | 'mismatch' | 'revoked' | 'untrusted' | 'unsupported'

/** The only shape the status IPC carries: public facts and plain words, never key material. */
export interface OwnerGrantStatus {
  state: OwnerGrantState
  canSign: boolean
  kid: string | null
  anchorKid: string | null
  refusal: string | null
  message: string
  busy: boolean
}

export interface OwnerGrantActionResult {
  ok: boolean
  reason?: string
  kid?: string
  unchanged?: boolean
  status: OwnerGrantStatus
}

type ActionOutcome = Omit<OwnerGrantActionResult, 'status'>

export interface OwnerGrantConfirmRequest {
  title: string
  message: string
  detail: string
}

export interface OwnerGrantControllerDeps {
  store: OwnerKeyStore
  /** Production: `() => readTrustedOwnerAnchor()` (the hard-coded root-owned path). */
  readAnchor: () => OwnerAnchorRead
  adminRunner: AdminRunner
  /** The native confirm from main. Resolves true only on an explicit yes. */
  confirm: (req: OwnerGrantConfirmRequest) => Promise<boolean>
  ownerUid: number
  grantsDir: string
  /** The `hermes_owner_grant` package directory the verifier files are read from. Without it
   *  (or with any file missing) every install is refused before a confirm or admin prompt. */
  verifierSourceDir?: string
  /** Production: `() => readInstalledOwnerVerifierManifest()`, the root-owned `manifest.sha256` the
   *  root script wrote beside the anchor, or null when it can't be read. With it, enable also
   *  re-pushes the verifier PACKAGE when an installed file's hash differs from this app's (b9 §6),
   *  and only reports success once the manifest says the package is current. Without it only the
   *  launcher sha is compared (the pre-b9 behaviour; tests that never install stay isolated). */
  readInstalledManifest?: () => Buffer | null
  tmpRoot?: string
  now?: () => number
  log?: OwnerKeyLog
  platform?: NodeJS.Platform
  fs?: typeof nodeFs
}

interface AnchorKeyDoc {
  kid: string
  alg: 'Ed25519'
  pub: string
  status: 'active' | 'retired' | 'revoked'
  not_before: number
  retired_at: number | null
}

interface AnchorDoc {
  format: typeof OWNER_ANCHOR_FORMAT
  owner_uid: number
  grants_dir: string
  keys: AnchorKeyDoc[]
  verifier_sha256: string
}

/** Staged relative path -> bytes, for every package file. */
type VerifierFiles = Map<string, Buffer>

export const OWNER_VERIFY_MANIFEST_PATH = `${OWNER_ANCHOR_DIR}/manifest.sha256`

/** Read the installed verifier manifest (root-owned, 0644). Null when it is missing, a symlink,
 *  not a regular file or too large. Only ever used to decide whether to offer a re-push, never to
 *  trust the verifier: a wrong answer costs one extra confirm or leaves the old package in place. */
export function readInstalledOwnerVerifierManifest(fs: typeof nodeFs = nodeFs): Buffer | null {
  try {
    const st = fs.lstatSync(OWNER_VERIFY_MANIFEST_PATH)

    if (st.isSymbolicLink() || !st.isFile() || st.size > MAX_VERIFIER_FILE_BYTES) {
      return null
    }

    const bytes = fs.readFileSync(OWNER_VERIFY_MANIFEST_PATH)

    return bytes.length > MAX_VERIFIER_FILE_BYTES ? null : bytes
  } catch {
    return null
  }
}

/** Whether an installed `manifest.sha256` (`<sha256>  <path>` lines) pins exactly this app's
 *  launcher and every package file. A missing, extra-malformed or differing line means "not
 *  current". The root-compiled `pycache/` lines are ignored (they follow from the package). */
export function installedVerifierIsCurrent(manifest: Buffer | string | null, verifier: VerifierFiles): boolean {
  if (manifest === null) {
    return false
  }

  const listed = new Map<string, string>()

  for (const line of manifest.toString().split('\n')) {
    const match = /^([0-9a-f]{64}) {2}(\S.*)$/.exec(line)

    if (match) {
      listed.set(match[2], match[1])
    }
  }

  const sha = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex')

  if (listed.get('hermes_owner_verify.py') !== OWNER_VERIFY_LAUNCHER_SHA256) {
    return false
  }

  return VERIFIER_PACKAGE_FILES.every(name => {
    const rel = `${VERIFIER_PACKAGE_REL}/${name}`
    const bytes = verifier.get(rel)

    return bytes !== undefined && listed.get(rel) === sha(bytes)
  })
}

const MESSAGES: Record<Exclude<OwnerGrantState, 'ready'>, string> = {
  off: 'Off. Owner decisions are not signed.',
  mismatch: "Owner-grant anchor doesn't match this app's key. Signing is off until you turn this on again.",
  revoked: 'The owner key was revoked. Signing is off until you turn this on again.',
  untrusted: 'Owner-grant anchor not installed or tampered. Signing is off until you turn this on again.',
  unsupported: 'Only available on macOS.'
}

const ADMIN_NOTE = 'Next, macOS will ask for an admin password once. Approve that prompt only now, right after this.'

function keyDoc(k: OwnerAnchorKey): AnchorKeyDoc {
  return {
    kid: k.kid,
    alg: 'Ed25519',
    pub: k.pub,
    status: k.status as AnchorKeyDoc['status'],
    not_before: k.notBefore,
    retired_at: k.retiredAt
  }
}

function sameAnchor(anchor: OwnerAnchor, doc: AnchorDoc): boolean {
  return (
    anchor.ownerUid === doc.owner_uid &&
    anchor.grantsDir === doc.grants_dir &&
    anchor.verifierSha256 === doc.verifier_sha256 &&
    anchor.keys.length === doc.keys.length &&
    anchor.keys.every((k, i) => {
      const want = doc.keys[i]

      return (
        k.kid === want.kid &&
        k.pub === want.pub &&
        k.status === want.status &&
        k.notBefore === want.not_before &&
        k.retiredAt === want.retired_at
      )
    })
  )
}

/** Keep the list within the anchor's key limit: drop the oldest retired keys first, then the
 *  oldest revoked ones (a dropped revoked kid still fails, as `unknown_kid`). */
function trimKeys(keys: AnchorKeyDoc[]): AnchorKeyDoc[] {
  const out = [...keys]

  for (const status of ['retired', 'revoked'] as const) {
    while (out.length > MAX_ANCHOR_KEYS) {
      const index = out.findIndex(k => k.status === status)

      if (index < 0) {
        break
      }

      out.splice(index, 1)
    }
  }

  return out
}

class OwnerGrantControllerImpl {
  readonly #d: OwnerGrantControllerDeps
  readonly #fs: typeof nodeFs
  #busy = false
  #refusal: string | null = null

  constructor(deps: OwnerGrantControllerDeps) {
    this.#d = deps
    this.#fs = deps.fs ?? nodeFs
  }

  #supported(): boolean {
    return (this.#d.platform ?? process.platform) === 'darwin'
  }

  #now(): number {
    return (this.#d.now ?? Date.now)()
  }

  #log(level: 'info' | 'warn' | 'error', message: string, meta?: Record<string, unknown>): void {
    this.#d.log?.(level, message, meta)
  }

  /** Launch: anchor first, then the blob only if the anchor pins it. Never generates a key. */
  launch(): OwnerGrantStatus {
    const read = this.#d.readAnchor()

    if (read.ok === false && read.reason !== 'anchor_missing') {
      this.#log('warn', 'owner-grant anchor not trusted', { detail: read.detail })
    }

    this.#d.store.setAnchor(read.ok ? read.anchor : null)
    this.#refusal = null

    try {
      this.#d.store.loadIfEnrolled()
    } catch (error) {
      // Fail closed: no key means no owner-grant signing this run. Only the code is kept.
      this.#refusal = error instanceof OwnerKeyError ? error.code : 'unexpected_error'
      this.#log('warn', 'owner-grant key not loaded', { code: this.#refusal })
    }

    return this.#statusFrom(read)
  }

  /** Fresh status. Re-reads the anchor, so the signing gate always follows the root file. */
  status(): OwnerGrantStatus {
    const read = this.#d.readAnchor()
    this.#d.store.setAnchor(read.ok ? read.anchor : null)

    return this.#statusFrom(read)
  }

  #statusFrom(read: OwnerAnchorRead): OwnerGrantStatus {
    const store = this.#d.store
    const kid = store.publicInfo()?.kid ?? null
    const active = read.ok ? (read.anchor.keys.find(k => k.status === 'active') ?? null) : null
    let state: OwnerGrantState

    if (!this.#supported()) {
      state = 'unsupported'
    } else if (read.ok === false) {
      state = read.reason === 'anchor_missing' ? 'off' : 'untrusted'
    } else if (store.anchorState() === 'match') {
      state = 'ready'
    } else {
      state = active ? 'mismatch' : 'revoked'
    }

    return {
      state,
      canSign: state === 'ready',
      kid,
      anchorKid: active?.kid ?? null,
      refusal: this.#refusal,
      message: state === 'ready' ? `On. Owner key ${kid}.` : MESSAGES[state],
      busy: this.#busy
    }
  }

  /** The IPC entry point: only the three known actions. */
  runAction(action: unknown): Promise<OwnerGrantActionResult> {
    if (action === 'enable') {
      return this.enable()
    }

    if (action === 'rotate') {
      return this.rotate()
    }

    if (action === 'revoke') {
      return this.revoke()
    }

    return Promise.resolve({ ok: false, reason: 'bad_action', status: this.status() })
  }

  /** "Let conductor verify owner decisions": pin a fresh key unless the anchor already pins ours.
   *  When it does but the installed verifier is not this app's launcher (an older install, or
   *  one from before U11b), or (b9 §6) the installed package's manifest differs from this app's
   *  package (a scope-catalog or verifier update with the same launcher), the same keys are
   *  re-installed with the current verifier: one confirm, one admin prompt. */
  enable(): Promise<OwnerGrantActionResult> {
    return this.#exclusive(async () => {
      const read = this.#d.readAnchor()
      this.#d.store.setAnchor(read.ok ? read.anchor : null)

      if (read.ok && this.#d.store.anchorState() === 'match') {
        const kid = this.#d.store.publicInfo()!.kid

        if (read.anchor.verifierSha256 === OWNER_VERIFY_LAUNCHER_SHA256 && this.#installedPackageCurrent()) {
          return { ok: true, kid, unchanged: true }
        }

        return this.#refreshVerifier(read.anchor, kid)
      }

      const verifier = this.#loadVerifier()

      if (!verifier) {
        return { ok: false, reason: 'verifier_unavailable' }
      }

      return this.#pinFreshKey('enable', read, verifier)
    })
  }

  /** New active key; the old one retired now (or revoked when this app doesn't hold it). */
  rotate(): Promise<OwnerGrantActionResult> {
    return this.#exclusive(async () => {
      const read = this.#d.readAnchor()
      this.#d.store.setAnchor(read.ok ? read.anchor : null)
      const verifier = this.#loadVerifier()

      if (!verifier) {
        return { ok: false, reason: 'verifier_unavailable' }
      }

      return this.#pinFreshKey('rotate', read, verifier)
    })
  }

  /** Revoke the anchor's active key: every grant under it fails `key_revoked` at once. */
  revoke(): Promise<OwnerGrantActionResult> {
    return this.#exclusive(async () => {
      const read = this.#d.readAnchor()
      this.#d.store.setAnchor(read.ok ? read.anchor : null)

      if (read.ok === false) {
        return { ok: false, reason: read.reason }
      }

      const target = read.anchor.keys.find(k => k.status === 'active')

      if (!target) {
        return { ok: false, reason: 'no_active_key' }
      }

      const verifier = this.#loadVerifier()

      if (!verifier) {
        return { ok: false, reason: 'verifier_unavailable' }
      }

      const doc = this.#doc(
        read.anchor.keys.map(k => (k.kid === target.kid ? { ...keyDoc(k), status: 'revoked' as const } : keyDoc(k)))
      )

      const confirmed = await this.#confirm({
        title: 'Revoke owner key',
        message: `Revoke owner key ${target.kid}?`,
        detail: `Every grant signed with ${target.kid} stops verifying at once, and signing stays off until you turn this on again. ${ADMIN_NOTE}`
      })

      if (!confirmed) {
        return { ok: false, reason: 'cancelled' }
      }

      const ran = await this.#install(doc, verifier)
      const after = this.#d.readAnchor()
      this.#d.store.setAnchor(after.ok ? after.anchor : null)

      if (!after.ok || !sameAnchor(after.anchor, doc)) {
        return { ok: false, reason: this.#runnerReason(ran) }
      }

      this.#log('info', 'owner-grant key revoked', { kid: target.kid })

      return { ok: true, kid: target.kid }
    })
  }

  // -- private -----------------------------------------------------------------------------------

  /** Same keys, current verifier: one confirm, one admin prompt, then the anchor must say so. */
  async #refreshVerifier(anchor: OwnerAnchor, kid: string): Promise<ActionOutcome> {
    const verifier = this.#loadVerifier()

    if (!verifier) {
      return { ok: false, reason: 'verifier_unavailable' }
    }

    const doc = this.#doc(anchor.keys.map(keyDoc))

    const confirmed = await this.#confirm({
      title: 'Update the owner-grant verifier',
      message: `Update the verifier for owner key ${kid}?`,
      detail: `Your owner key stays the same; only the root-owned verifier conductor runs is replaced. ${ADMIN_NOTE}`
    })

    if (!confirmed) {
      return { ok: false, reason: 'cancelled' }
    }

    const ran = await this.#install(doc, verifier)
    const after = this.#d.readAnchor()
    this.#d.store.setAnchor(after.ok ? after.anchor : null)

    // A package-only refresh leaves anchor.json byte-identical, so the anchor alone can't prove the
    // install happened: the installed manifest must now pin this app's package too.
    const packageLanded = !this.#d.readInstalledManifest || installedVerifierIsCurrent(this.#d.readInstalledManifest(), verifier)

    if (!after.ok || !sameAnchor(after.anchor, doc) || !packageLanded) {
      return { ok: false, reason: this.#runnerReason(ran) }
    }

    this.#log('info', 'owner-grant verifier updated', { kid })

    return { ok: true, kid }
  }

  /** True unless the installed package is known to differ from this app's. Without the manifest
   *  reader, or when this app's own package can't be read (nothing to push), it can't tell: true. */
  #installedPackageCurrent(): boolean {
    const readManifest = this.#d.readInstalledManifest

    if (!readManifest) {
      return true
    }

    const verifier = this.#loadVerifier()

    return verifier === null || installedVerifierIsCurrent(readManifest(), verifier)
  }

  /** The package files, read before any confirm or key work. Null when any is missing, not a
   *  regular file, or too large: the install then never starts. */
  #loadVerifier(): VerifierFiles | null {
    const dir = this.#d.verifierSourceDir

    if (!dir) {
      return null
    }

    const files: VerifierFiles = new Map()

    try {
      for (const name of VERIFIER_PACKAGE_FILES) {
        const file = path.join(dir, name)
        const st = this.#fs.statSync(file)

        if (!st.isFile() || st.size > MAX_VERIFIER_FILE_BYTES) {
          return null
        }

        const bytes = this.#fs.readFileSync(file)

        if (bytes.length > MAX_VERIFIER_FILE_BYTES) {
          return null
        }

        files.set(`${VERIFIER_PACKAGE_REL}/${name}`, bytes)
      }
    } catch {
      return null
    }

    return files
  }

  async #pinFreshKey(mode: 'enable' | 'rotate', read: OwnerAnchorRead, verifier: VerifierFiles): Promise<ActionOutcome> {
    const store = this.#d.store
    const loaded: OwnerKeyPublic | null = store.publicInfo()
    // D20: always a brand-new key; the on-disk blob is not consulted.
    const fresh = store.beginFreshKey()
    const now = this.#now()
    let previous: { kid: string; status: 'retired' | 'revoked' } | null = null
    const carried: AnchorKeyDoc[] = []

    // Keys from an untrusted anchor are never carried: re-enabling over it starts clean.
    if (read.ok) {
      for (const k of read.anchor.keys) {
        if (k.status !== 'active') {
          carried.push(keyDoc(k))
        } else if (loaded && loaded.kid === k.kid && loaded.pub === k.pub) {
          carried.push({ ...keyDoc(k), status: 'retired', retired_at: now })
          previous = { kid: k.kid, status: 'retired' }
        } else {
          // An active key this app does not hold (lost key, swapped blob, phished install) can't
          // be vouched for: revoke it rather than let it keep verifying as "retired".
          carried.push({ ...keyDoc(k), status: 'revoked' })
          previous = { kid: k.kid, status: 'revoked' }
        }
      }
    }

    const doc = this.#doc(
      trimKeys([
        ...carried,
        { kid: fresh.kid, alg: 'Ed25519', pub: fresh.pub, status: 'active', not_before: now, retired_at: null }
      ])
    )

    const replaced = previous
      ? previous.status === 'retired'
        ? `The current key ${previous.kid} is retired: grants it already signed keep working until they expire. `
        : `The current key ${previous.kid} is not this app's key, so it is revoked. `
      : ''

    const confirmed = await this.#confirm(
      mode === 'enable' && !previous
        ? {
            title: 'Let conductor verify owner decisions',
            message: `Pin new owner key ${fresh.kid}?`,
            detail: `Hermes made a new owner key, ${fresh.kid}, to sign your decisions so conductor can check them. ${ADMIN_NOTE}`
          }
        : {
            title: 'Replace owner key',
            message: `Replace the owner key with ${fresh.kid}?`,
            detail: `${replaced}${ADMIN_NOTE}`
          }
    )

    if (!confirmed) {
      store.discardPendingKey()

      return { ok: false, reason: 'cancelled' }
    }

    try {
      store.stagePendingKey()
    } catch (error) {
      store.discardPendingKey()

      return { ok: false, reason: error instanceof OwnerKeyError ? error.code : 'key_save_failed' }
    }

    const ran = await this.#install(doc, verifier)
    const after = this.#d.readAnchor()
    store.setAnchor(after.ok ? after.anchor : null)
    const active = after.ok ? after.anchor.keys.find(k => k.status === 'active') : undefined

    if (!after.ok || !sameAnchor(after.anchor, doc) || active?.kid !== fresh.kid || active.pub !== fresh.pub) {
      store.discardPendingKey()
      const reason = this.#runnerReason(ran)
      this.#log('warn', 'owner-grant anchor was not pinned', { reason, kid: fresh.kid })

      return { ok: false, reason }
    }

    try {
      store.commitPendingKey()
    } catch (error) {
      store.discardPendingKey()
      this.#log('error', 'owner-grant key could not be saved after pinning', {
        code: error instanceof OwnerKeyError ? error.code : 'io_error'
      })

      return { ok: false, reason: 'key_save_failed' }
    }

    this.#refusal = null

    return { ok: true, kid: fresh.kid }
  }

  #doc(keys: AnchorKeyDoc[]): AnchorDoc {
    return {
      format: OWNER_ANCHOR_FORMAT,
      owner_uid: this.#d.ownerUid,
      grants_dir: this.#d.grantsDir,
      keys,
      verifier_sha256: OWNER_VERIFY_LAUNCHER_SHA256
    }
  }

  async #confirm(req: OwnerGrantConfirmRequest): Promise<boolean> {
    try {
      return (await this.#d.confirm(req)) === true
    } catch {
      return false
    }
  }

  /** Stage the payload privately (0700 dirs, 0600 files), run the ONE admin command, and always
   *  clean up. The manifest pins every staged file; argv pins the manifest. */
  async #install(doc: AnchorDoc, verifier: VerifierFiles): Promise<AdminRunResult & { error?: string }> {
    const fs = this.#fs
    const payload = new Map<string, Buffer>([
      ['anchor.json', Buffer.from(JSON.stringify(doc), 'utf8')],
      ['hermes_owner_verify.py', Buffer.from(OWNER_VERIFY_LAUNCHER, 'utf8')],
      ...verifier
    ])
    const sha = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex')
    let dir: string | null = null

    try {
      if (STAGED_PAYLOAD_FILES.some(rel => !payload.has(rel)) || payload.size !== STAGED_PAYLOAD_FILES.length) {
        throw new AnchorInstallError('the verifier payload is incomplete')
      }

      const manifest = Buffer.from(STAGED_PAYLOAD_FILES.map(rel => `${sha(payload.get(rel)!)}  ${rel}\n`).join(''), 'ascii')
      dir = fs.mkdtempSync(path.join(this.#d.tmpRoot ?? os.tmpdir(), 'hermes-owner-anchor-'))
      fs.chmodSync(dir, 0o700)

      for (const sub of ['verifier', VERIFIER_PACKAGE_REL]) {
        fs.mkdirSync(path.join(dir, sub), { mode: 0o700 })
        fs.chmodSync(path.join(dir, sub), 0o700)
      }

      for (const [rel, bytes] of [...payload, ['manifest.sha256', manifest] as const]) {
        const fd = fs.openSync(path.join(dir, rel), 'wx', 0o600)

        try {
          fs.writeSync(fd, bytes)
          fs.fsyncSync(fd)
        } finally {
          fs.closeSync(fd)
        }
      }

      return await this.#d.adminRunner.run(buildAdminArgv(path.join(dir, 'manifest.sha256'), sha(manifest)))
    } catch (error) {
      return { code: 1, cancelled: false, error: error instanceof AnchorInstallError ? error.code : 'stage_failed' }
    } finally {
      if (dir) {
        fs.rmSync(dir, { recursive: true, force: true })
      }
    }
  }

  #runnerReason(ran: AdminRunResult & { error?: string }): string {
    if (ran.error) {
      return ran.error
    }

    if (ran.cancelled) {
      return 'cancelled'
    }

    return ran.code === 0 ? 'not_pinned' : 'admin_failed'
  }

  /** One flow at a time; the status is taken after the flow ends, so `busy` is accurate. */
  async #exclusive(fn: () => Promise<ActionOutcome>): Promise<OwnerGrantActionResult> {
    if (this.#busy) {
      return { ok: false, reason: 'busy', status: this.status() }
    }

    if (!this.#supported()) {
      return { ok: false, reason: 'unsupported', status: this.status() }
    }

    this.#busy = true
    let outcome: ActionOutcome

    try {
      outcome = await fn()
    } catch (error) {
      this.#d.store.discardPendingKey()
      this.#log('error', 'owner-grant flow failed', { code: error instanceof OwnerKeyError ? error.code : 'unexpected_error' })
      outcome = { ok: false, reason: 'unexpected_error' }
    } finally {
      this.#busy = false
    }

    return { ...outcome, status: this.status() }
  }
}

export type OwnerGrantController = OwnerGrantControllerImpl

export function createOwnerGrantController(deps: OwnerGrantControllerDeps): OwnerGrantController {
  return new OwnerGrantControllerImpl(deps)
}
