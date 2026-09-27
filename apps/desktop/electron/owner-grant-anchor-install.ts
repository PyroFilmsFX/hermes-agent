/**
 * Install, rotate and revoke the root-owned owner-grant anchor, and the launch check that refuses
 * to sign on a mismatch (#60 U11; VERIFY addendum §1.4, §1.5; E-9, T-3, T-4; decision D20).
 *
 * The privileged step is ONE macOS admin prompt:
 *
 *   /usr/bin/osascript -e 'on run argv' -e '<ADMIN_APPLESCRIPT[1]>' -e 'end run' \
 *     <ANCHOR_ROOT_SCRIPT> <staged anchor file> <sha256 of its bytes>
 *
 * Every element of that argv is a constant in this file except the last two, and those are
 * validated (a mkdtemp path of a fixed shape, 64 lowercase hex) before anything runs. osascript
 * is exec'd directly (no shell). The AppleScript is a constant that hands its three arguments to
 * `/bin/sh -c` through `quoted form of`, so nothing is ever interpolated into a command line. The
 * anchor JSON itself travels in a file this app creates (0600 in a 0700 mkdtemp); the root script
 * copies it into a root-owned 0600 temp inside the anchor directory, checks the sha256 of THAT
 * copy (so rewriting the staged file after hashing fails closed), then chmods 0644 and renames it
 * into place. Directories are root:wheel 0755. After the runner returns, main re-reads the anchor
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

import { OWNER_ANCHOR_FORMAT, type OwnerAnchor, type OwnerAnchorKey, type OwnerAnchorRead } from './owner-grant-anchor'
import { OwnerKeyError, type OwnerKeyLog, type OwnerKeyPublic, type OwnerKeyStore } from './owner-grant-key'

export const OSASCRIPT_PATH = '/usr/bin/osascript'
const MAX_ANCHOR_KEYS = 32

/** The AppleScript program, one `-e` per line. Its arguments are (1) the root script, (2) the
 *  staged file, (3) the sha256; each reaches sh as one word via `quoted form of`. */
export const ADMIN_APPLESCRIPT: readonly [string, string, string] = Object.freeze([
  'on run argv',
  'do shell script "/bin/sh -c " & quoted form of (item 1 of argv) & " hermes-owner-anchor " & quoted form of (item 2 of argv) & " " & quoted form of (item 3 of argv) with prompt "Hermes wants to pin your owner key so conductor can verify owner decisions." with administrator privileges',
  'end run'
]) as readonly [string, string, string]

/** Runs as root: `sh -c ANCHOR_ROOT_SCRIPT hermes-owner-anchor <staged file> <sha256>`. */
export const ANCHOR_ROOT_SCRIPT = `set -eu
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077
src=$1
want=$2
top='/Library/Application Support/Hermes'
dir="$top/owner-grant"
tmp="$dir/.anchor.json.$$"
case $want in
  *[!0-9a-f]*) exit 64 ;;
esac
[ "\${#want}" -eq 64 ] || exit 64
[ -f "$src" ] || exit 66
[ ! -L "$src" ] || exit 66
for d in "$top" "$dir"; do
  [ ! -L "$d" ] || exit 73
  [ -d "$d" ] || /bin/mkdir "$d"
  /usr/sbin/chown root:wheel "$d"
  /bin/chmod 0755 "$d"
done
trap '/bin/rm -f "$tmp"' EXIT
/bin/rm -f "$tmp"
/usr/bin/head -c 65536 "$src" > "$tmp"
/usr/sbin/chown root:wheel "$tmp"
got=$(/usr/bin/shasum -a 256 "$tmp")
got=\${got%% *}
[ "$got" = "$want" ] || exit 65
/bin/chmod 0644 "$tmp"
/bin/mv -f "$tmp" "$dir/anchor.json"
`

const STAGED_FILE_RE = /^\/(?:[A-Za-z0-9._+-]+\/)*hermes-owner-anchor-[A-Za-z0-9]{6}\/anchor\.json$/
const SHA256_RE = /^[0-9a-f]{64}$/

export class AnchorInstallError extends Error {
  readonly code = 'bad_argument'
}

/** The full osascript argv. Throws unless the two variable parts have their fixed shapes. */
export function buildAdminArgv(stagedFile: string, sha256: string): string[] {
  if (
    typeof stagedFile !== 'string' ||
    !STAGED_FILE_RE.test(stagedFile) ||
    path.posix.normalize(stagedFile) !== stagedFile ||
    stagedFile.split('/').some(part => part === '.' || part === '..')
  ) {
    throw new AnchorInstallError('the staged anchor path has an unexpected shape')
  }

  if (typeof sha256 !== 'string' || !SHA256_RE.test(sha256)) {
    throw new AnchorInstallError('the anchor digest is not 64 lowercase hex')
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

  /** "Let conductor verify owner decisions": pin a fresh key unless the anchor already pins ours. */
  enable(): Promise<OwnerGrantActionResult> {
    return this.#exclusive(async () => {
      const read = this.#d.readAnchor()
      this.#d.store.setAnchor(read.ok ? read.anchor : null)

      if (read.ok && this.#d.store.anchorState() === 'match') {
        return { ok: true, kid: this.#d.store.publicInfo()!.kid, unchanged: true }
      }

      return this.#pinFreshKey('enable', read)
    })
  }

  /** New active key; the old one retired now (or revoked when this app doesn't hold it). */
  rotate(): Promise<OwnerGrantActionResult> {
    return this.#exclusive(async () => {
      const read = this.#d.readAnchor()
      this.#d.store.setAnchor(read.ok ? read.anchor : null)

      return this.#pinFreshKey('rotate', read)
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

      const ran = await this.#install(doc)
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

  async #pinFreshKey(mode: 'enable' | 'rotate', read: OwnerAnchorRead): Promise<ActionOutcome> {
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

    const ran = await this.#install(doc)
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
    return { format: OWNER_ANCHOR_FORMAT, owner_uid: this.#d.ownerUid, grants_dir: this.#d.grantsDir, keys }
  }

  async #confirm(req: OwnerGrantConfirmRequest): Promise<boolean> {
    try {
      return (await this.#d.confirm(req)) === true
    } catch {
      return false
    }
  }

  /** Stage the anchor bytes privately, run the ONE admin command, and always clean up. */
  async #install(doc: AnchorDoc): Promise<AdminRunResult & { error?: string }> {
    const fs = this.#fs
    const bytes = Buffer.from(JSON.stringify(doc), 'utf8')
    const sha256 = createHash('sha256').update(bytes).digest('hex')
    let dir: string | null = null

    try {
      dir = fs.mkdtempSync(path.join(this.#d.tmpRoot ?? os.tmpdir(), 'hermes-owner-anchor-'))
      fs.chmodSync(dir, 0o700)
      const file = path.join(dir, 'anchor.json')
      const fd = fs.openSync(file, 'wx', 0o600)

      try {
        fs.writeSync(fd, bytes)
        fs.fsyncSync(fd)
      } finally {
        fs.closeSync(fd)
      }

      return await this.#d.adminRunner.run(buildAdminArgv(file, sha256))
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
