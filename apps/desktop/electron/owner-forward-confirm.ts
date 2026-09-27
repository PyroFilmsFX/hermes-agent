/**
 * #60 owner-forward, main-process adapters (design §2/§4, VERIFY addendum U12): the IPC handler
 * behind `hermes:owner-forward:confirm`, the real native confirm (built here, shown through an
 * injected `showMessageBox`), Touch ID for prod scopes (injected `touchId`), and the rate gates.
 *
 * What main trusts from the renderer: nothing but ids and the text. Titles are resolved HERE with
 * main's own backend token (`resolveSession`); renderer labels are never read. The signing core
 * (`owner-grant-sign.ts`) validates, builds the confirm model, runs the dialog and Touch ID
 * ports, and writes the grant files before any envelope leaves main.
 *
 * Gates, in order: the sender must be an app window's main frame (never a widget/preview subframe
 * or a webview guest), then one open dialog at a time, a 3 s cooldown after Cancel, and at most
 * 10 confirms per rolling minute. The admin action IPC (`hermes:owner-grant:action`) gets the same
 * sender check and its own gate (`createOwnerGrantActionHandler`).
 *
 * Fix round (#60 wave): every target's LIVE Claude CLI session id is signed (T-6; a conductor scope
 * with an unbound target is refused by the core), `source_session.role` comes from main's own
 * lookup of the origin row, titles are sanitized here, and a text longer than the dialog shows
 * whole needs a "View full text" step (a 0600 file in an app-owned 0700 dir, opened outside any
 * webContents) before Send is offered.
 */

import { randomBytes as nodeRandomBytes } from 'node:crypto'
import nodeFs from 'node:fs'
import path from 'node:path'

import {
  confirmAndSignGrants,
  type ConfirmModel,
  type Gesture,
  GESTURES,
  OwnerGrantSignError,
  parseScope,
  type SignedOutcome,
  type SignPorts,
  type SignRequest
} from './owner-grant-sign'

export const DIALOG_TEXT_FULL_MAX = 3000
export const DIALOG_TEXT_HEAD = 1400
export const DIALOG_TEXT_TAIL = 1000
export const CANCEL_COOLDOWN_MS = 3000
export const MAX_CONFIRMS_PER_MINUTE = 10
export const DIALOG_TITLE_MAX = 80
export const SEND_BUTTON = 'Send'
export const VIEW_BUTTON = 'View full text'
export const CANCEL_BUTTON = 'Cancel'
/** Re-showings of one confirm (each View re-shows it); past this the confirm counts as cancelled. */
export const MAX_DIALOG_SHOWINGS = 6
const MINUTE_MS = 60_000

// -- text shown in the native dialog -----------------------------------------------------------

// Bidi embeddings/overrides/isolates and marks, zero-width and invisible formatting characters,
// variation selectors, C0/C1 controls (incl. ESC), and the Unicode line/paragraph separators.
const INVISIBLE_RANGES: ReadonlyArray<readonly [number, number]> = [
  [0x0000, 0x001f],
  [0x007f, 0x009f],
  [0x00ad, 0x00ad],
  [0x061c, 0x061c],
  [0x115f, 0x1160],
  [0x17b4, 0x17b5],
  [0x180b, 0x180f],
  [0x200b, 0x200f],
  [0x2028, 0x202e],
  [0x2060, 0x206f],
  [0x3164, 0x3164],
  [0xfe00, 0xfe0f],
  [0xfeff, 0xfeff],
  [0xffa0, 0xffa0],
  [0xfff9, 0xfffb],
  [0xe0000, 0xe007f]
]

function isInvisible(cp: number): boolean {
  return INVISIBLE_RANGES.some(([lo, hi]) => cp >= lo && cp <= hi)
}

/** A backend-supplied label (session title) as the native dialog may show it: no bidi or invisible
 *  characters, no controls, whitespace collapsed, clamped to `max` characters. Done in MAIN. */
export function sanitizeDialogTitle(raw: unknown, max = DIALOG_TITLE_MAX): string {
  if (typeof raw !== 'string') {
    return ''
  }

  const spaced = raw.replace(/[\t\n\v\f\r\u0085\u2028\u2029]/gu, ' ')

  const visible = Array.from(spaced)
    .filter(ch => !isInvisible(ch.codePointAt(0) ?? 0))
    .join('')

  const clean = visible.replace(/\s+/gu, ' ').trim()
  const chars = Array.from(clean)


  return chars.length > max ? `${chars.slice(0, max - 1).join('')}…` : clean
}

// -- sender check ------------------------------------------------------------------------------

interface FrameLike {
  parent?: unknown
}

interface IpcEventLike {
  sender?: { mainFrame?: unknown; getType?: () => string } | null
  senderFrame?: FrameLike | null
}

/** True only for the top-level frame of an app window's webContents (app chrome). */
export function isAppChromeSender(event: unknown, isAppWebContents: (sender: unknown) => boolean): boolean {
  const e = event as IpcEventLike | null

  if (!e || !e.sender || !e.senderFrame) {
    return false
  }

  const sender = e.sender

  try {
    if (typeof sender.getType === 'function' && sender.getType() !== 'window') {
      return false
    }
  } catch {
    return false
  }

  if (!isAppWebContents(sender)) {
    return false
  }

  return e.senderFrame === sender.mainFrame && !e.senderFrame.parent
}

// -- rate gate ---------------------------------------------------------------------------------

export type RateRefusal = 'cooldown' | 'dialog_open' | 'rate'

export function createConfirmRateGate(now: () => number) {
  let open = false
  let cooldownUntil = 0
  const starts: number[] = []

  return {
    tryOpen(): { ok: true } | { ok: false; reason: RateRefusal } {
      const t = now()

      if (open) {
        return { ok: false, reason: 'dialog_open' }
      }

      if (t < cooldownUntil) {
        return { ok: false, reason: 'cooldown' }
      }

      while (starts.length && t - starts[0] >= MINUTE_MS) {
        starts.shift()
      }

      if (starts.length >= MAX_CONFIRMS_PER_MINUTE) {
        return { ok: false, reason: 'rate' }
      }

      starts.push(t)
      open = true

      return { ok: true }
    },
    close(cancelled: boolean): void {
      open = false

      if (cancelled) {
        cooldownUntil = now() + CANCEL_COOLDOWN_MS
      }
    }
  }
}

// -- dialog ------------------------------------------------------------------------------------

export type ClaudeSessionState = 'live' | 'not_running' | 'starting' | 'unknown'
export type SourceRole = 'assistant' | 'peer' | 'user'

export interface ConfirmDialogExtras {
  /** The owner opened the full text in this confirm (long texts only offer Send after that). */
  fullTextViewed?: boolean
  /** Main's own title for the origin session (sanitized here). */
  sourceTitle?: string | null
  /** Per `<profile>:<session_id>`: why a target's claude_session_id is null. */
  claudeStates?: Record<string, ClaudeSessionState>
  checkboxChecked?: boolean
}

export interface ConfirmDialogOptions {
  type: 'question' | 'warning'
  title: string
  message: string
  detail: string
  buttons: string[]
  defaultId: number
  cancelId: number
  noLink: true
  checkboxLabel?: string
  checkboxChecked?: boolean
}

const GESTURE_LABEL: Record<Gesture, string> = {
  composer_signed: 'typed in this chat',
  menu: 'Forward to…',
  proposal: 'manager proposal',
  selection: 'selected text',
  slash_to: '/to'
}

function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 12)}…` : id
}

const ROLE_LABEL: Record<SourceRole, string> = {
  assistant: 'written by the agent',
  peer: 'a message from another session',
  user: 'typed by you'
}

function bindingLine(claudeId: string | null, state: ClaudeSessionState | undefined): string {
  if (claudeId) {
    return `Claude session ${shortId(claudeId)}`
  }

  if (state === 'not_running') {
    return 'not bound: its Claude CLI is not running'
  }

  if (state === 'starting') {
    return 'not bound: its Claude CLI session id is not known yet'
  }

  return 'not bound: its Claude CLI session id is not known'
}

export function isLongText(text: string): boolean {
  return text.length > DIALOG_TEXT_FULL_MAX
}

function dialogText(model: Pick<ConfirmModel, 'text' | 'textChars'>): string {
  const text = model.text

  if (text.length <= DIALOG_TEXT_FULL_MAX) {
    return text
  }

  const head = text.slice(0, DIALOG_TEXT_HEAD)
  const tail = text.slice(text.length - DIALOG_TEXT_TAIL)
  const omitted = text.length - head.length - tail.length

  return `${head}\n\n[… ${omitted} characters omitted …]\n\n${tail}`
}

/** The native confirm's options: every target title main resolved, every scope with its class and
 *  expiry, then the text (whole up to 3000 characters, else head + tail with counts). */
export function buildConfirmDialog(
  model: ConfirmModel,
  formatTime: (ms: number) => string = ms => new Date(ms).toLocaleString(),
  extras: ConfirmDialogExtras = {}
): ConfirmDialogOptions {
  const lines: string[] = []
  lines.push(model.targets.length === 1 ? 'To:' : `To (${model.targets.length}):`)

  for (const t of model.targets) {
    const title = sanitizeDialogTitle(t.title) || 'Untitled session'
    const state = extras.claudeStates?.[`${t.profile}:${t.session_id}`]
    lines.push(`  • ${title} (${t.profile}, ${shortId(t.session_id)}) · ${bindingLine(t.claude_session_id, state)}`)
  }

  lines.push('')

  if (model.quoteOnly) {
    lines.push(`Scope: none (a quotable decision), expires ${formatTime(model.expiresAt)}`)
  } else {
    lines.push('Scopes:')

    for (const s of model.scopes) {
      const name = s.catalogued && s.label ? s.label : 'Unrecognized scope'
      const use = s.singleUse ? 'single use' : 'reusable'
      const subject = s.subject ? `, subject ${s.subject}` : ''
      lines.push(`  • ${name}: ${s.scope} (${s.scopeClass}, ${use}${subject}), expires ${formatTime(s.expiresAt)}`)
    }
  }

  if (model.scopes.some(s => s.scopeClass === 'prod')) {
    lines.push('')
    lines.push(
      model.requiresTouchId
        ? 'Production scope: Touch ID is required after Send.'
        : 'Production scope: this signs a production action.'
    )
  }

  lines.push('')

  const counts =
    model.textBytes !== undefined && model.textBytes !== model.textChars
      ? `${model.textChars} characters, ${model.textBytes} bytes`
      : `${model.textChars} characters`

  lines.push(`Text (${counts}):`)
  lines.push(dialogText(model))

  const long = isLongText(model.text)

  if (long) {
    lines.push('')
    lines.push(
      extras.fullTextViewed
        ? 'You opened the full text in your text editor: that file is exactly the text that will be signed.'
        : `The middle of this text is hidden here. Choose ${VIEW_BUTTON} to read all of it in your text editor; Send is offered after that.`
    )
  }

  if (model.gesture) {
    lines.push('')
    lines.push(`From: ${GESTURE_LABEL[model.gesture] ?? model.gesture}`)
  }

  if (model.sourceSession) {
    const where = sanitizeDialogTitle(extras.sourceTitle) || shortId(model.sourceSession.session_id)
    const role = model.sourceSession.role
    lines.push(`Source: ${where} · ${role ? ROLE_LABEL[role] : 'author not known'}`)
  }

  const prod = model.scopes.some(s => s.scopeClass === 'prod')
  // A long text offers no Send until the owner viewed it in THIS confirm (per request).
  const buttons = long ? (extras.fullTextViewed ? [SEND_BUTTON, VIEW_BUTTON, CANCEL_BUTTON] : [VIEW_BUTTON, CANCEL_BUTTON]) : [SEND_BUTTON, CANCEL_BUTTON]

  return {
    type: prod || model.requiresUnrecognizedAck ? 'warning' : 'question',
    title: 'Send as you',
    message: 'Send as you?',
    detail: lines.join('\n'),
    buttons,
    defaultId: 0,
    cancelId: buttons.length - 1,
    noLink: true,
    ...(model.requiresUnrecognizedAck
      ? { checkboxLabel: 'Sign the unrecognized scope listed above', checkboxChecked: extras.checkboxChecked === true }
      : {})
  }
}

// -- "View full text" ----------------------------------------------------------------------------

export class FullTextViewError extends Error {}

/** Write `content` to a fresh 0600 file (O_EXCL, O_NOFOLLOW) in `dir`, an app-owned directory that
 *  is created 0700, must be a real directory (never a symlink) owned by this uid, and is tightened to
 *  0700 when looser. Returns the file path. */
export function writeFullTextFile(
  dir: string,
  content: string,
  opts: { fs?: typeof nodeFs; randomBytes?: (n: number) => Buffer } = {}
): string {
  const f = opts.fs ?? nodeFs

  try {
    f.mkdirSync(dir, { recursive: true, mode: 0o700 })
    const st = f.lstatSync(dir)
    const uid = process.getuid?.()

    if (st.isSymbolicLink() || !st.isDirectory()) {
      throw new FullTextViewError('the view folder is not a plain directory')
    }

    if (uid !== undefined && st.uid !== uid) {
      throw new FullTextViewError('the view folder is not owned by this user')
    }

    if ((st.mode & 0o777) !== 0o700) {
      f.chmodSync(dir, 0o700)
    }

    const file = path.join(dir, `forward-${(opts.randomBytes ?? nodeRandomBytes)(12).toString('hex')}.txt`)
    const c = f.constants
    const fd = f.openSync(file, c.O_WRONLY | c.O_CREAT | c.O_EXCL | (c.O_NOFOLLOW ?? 0), 0o600)

    try {
      f.writeSync(fd, content)
      f.fchmodSync(fd, 0o600)
    } finally {
      f.closeSync(fd)
    }

    return file
  } catch (error) {
    if (error instanceof FullTextViewError) {
      throw error
    }

    throw new FullTextViewError(`could not write the full text: ${error instanceof Error ? error.message : String(error)}`)
  }
}

function fullTextDocument(model: ConfirmModel): string {
  return [
    `Send as you? The text below is exactly what will be signed (${model.textChars} characters).`,
    'Reading copy only: editing or saving this file changes nothing. Go back to the dialog to Send or Cancel.',
    '────────────────────────────────────────',
    model.text
  ].join('\n')
}

// -- handler -----------------------------------------------------------------------------------

export interface ResolvedSession {
  title: string | null
  /** The LIVE Claude CLI session id the backend reported (T-6), else null. */
  claude_session_id: string | null
  claude_session_state?: ClaudeSessionState
  /** Only when a message id was asked for: the stored row's author class, else null. */
  message_role?: SourceRole | null
}

export interface OwnerForwardConfirmDeps {
  isTrustedSender: (event: unknown) => boolean
  /** The `HERMES_OWNER_GRANT_BACKEND` main handed the backend serving this profile, or null. */
  backendIdForProfile: (profile: string) => string | null
  /** Main's own lookup (`/api/sessions/{id}` with main's token, through the delivering backend's
   *  profile). null = no such session. With `messageId`, also the stored row's role. */
  resolveSession: (
    profile: string,
    sessionId: string,
    backendProfile: string,
    opts?: { messageId: string | null }
  ) => Promise<ResolvedSession | null>
  showMessageBox: (options: ConfirmDialogOptions) => Promise<{ response: number; checkboxChecked?: boolean }>
  /** App-owned directory for "View full text" copies (created 0700). */
  fullTextDir?: string
  /** `shell.openPath`: opens the file in the owner's default app, outside any webContents. Resolves
   *  to '' on success, else an error string. */
  openPath?: (file: string) => Promise<string>
  touchId?: SignPorts['touchId']
  store: SignPorts['store']
  grantsDir: string
  ownerUid: number
  now: () => number
  formatTime?: (ms: number) => string
  randomBytes?: SignPorts['randomBytes']
  fs?: SignPorts['fs']
  log?: (message: string) => void
}

export type OwnerForwardConfirmResult =
  | {
      ok: true
      decisionId: string
      grantId: string
      envelope: { format: string; kid: string; payload: string; sig: string }
      /** `<profile>:<session_id>` list the gateway's owner.forward binds delivery to. */
      targets: string[]
    }
  | { ok: false; cancelled: true; reason: 'dialog' | 'touch_id' | 'unrecognized_scope' }
  | { ok: false; code: string; error: string }

/** addendum §2.2: the role main signs. With a message id it is the stored row's (backend lookup);
 *  without one it follows the gesture main is confirming, never the renderer's role claim. */
export function deriveSourceRole(gesture: Gesture, messageId: string | null, storedRole: SourceRole | null | undefined): SourceRole | null {
  if (messageId !== null) {
    return storedRole ?? null
  }

  if (gesture === 'composer_signed' || gesture === 'slash_to') {
    return 'user'
  }

  return gesture === 'proposal' ? 'assistant' : null
}

const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const SESSION_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/
const ROLES = new Set(['assistant', 'peer', 'user'])

interface ParsedRequest {
  text: string
  gesture: Gesture
  origin: SignRequest['sourceSession']
  profile: string
  targets: Array<{ profile: string; session_id: string }>
  scope: string[]
  subject: string | null
  ttlMs?: number
}

function refuse(code: string, error: string): OwnerForwardConfirmResult {
  return { ok: false, code, error }
}

/** Strict shape check. Only ids, the text, scopes, subject and TTL are read; anything else the
 *  renderer sends (titles, labels, backend ids) is ignored. */
function parseRequest(raw: unknown): ParsedRequest | string {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return 'the request must be an object'
  }

  const r = raw as Record<string, unknown>

  if (typeof r.text !== 'string') {
    return 'text must be a string'
  }

  if (typeof r.gesture !== 'string' || !(GESTURES as readonly string[]).includes(r.gesture)) {
    return 'unknown gesture'
  }

  const o = r.origin as Record<string, unknown> | null

  if (
    !o ||
    typeof o !== 'object' ||
    typeof o.session_id !== 'string' ||
    !SESSION_RE.test(o.session_id) ||
    !(o.message_id === null || o.message_id === undefined || (typeof o.message_id === 'string' && o.message_id)) ||
    !(o.role === null || o.role === undefined || (typeof o.role === 'string' && ROLES.has(o.role)))
  ) {
    return 'origin needs a session id'
  }

  if (typeof r.profile !== 'string' || !PROFILE_RE.test(r.profile)) {
    return 'profile is invalid'
  }

  if (!Array.isArray(r.targets) || r.targets.length === 0) {
    return 'targets must be a non-empty list'
  }

  const targets: ParsedRequest['targets'] = []

  for (const t of r.targets as unknown[]) {
    const tt = t as Record<string, unknown> | null

    if (
      !tt ||
      typeof tt.profile !== 'string' ||
      !PROFILE_RE.test(tt.profile) ||
      typeof tt.session_id !== 'string' ||
      !SESSION_RE.test(tt.session_id)
    ) {
      return 'each target needs a profile and a session id'
    }

    targets.push({ profile: tt.profile, session_id: tt.session_id })
  }

  const scope = r.scope === undefined ? [] : r.scope

  if (!Array.isArray(scope) || !scope.every(s => typeof s === 'string')) {
    return 'scope must be a list of strings'
  }

  if (r.subject !== undefined && r.subject !== null && typeof r.subject !== 'string') {
    return 'subject must be a string'
  }

  if (r.ttlMs !== undefined && (typeof r.ttlMs !== 'number' || !Number.isSafeInteger(r.ttlMs) || r.ttlMs <= 0)) {
    return 'ttlMs must be a positive integer'
  }

  return {
    text: r.text,
    gesture: r.gesture as Gesture,
    origin: {
      session_id: o.session_id,
      message_id: typeof o.message_id === 'string' ? o.message_id : null,
      role: typeof o.role === 'string' ? (o.role as 'assistant' | 'peer' | 'user') : null
    },
    profile: r.profile,
    targets,
    scope: scope as string[],
    subject: typeof r.subject === 'string' && r.subject.trim() ? r.subject.trim() : null,
    ...(r.ttlMs !== undefined ? { ttlMs: r.ttlMs as number } : {})
  }
}

export function createOwnerForwardConfirmHandler(deps: OwnerForwardConfirmDeps) {
  const gate = createConfirmRateGate(deps.now)

  return async function handleOwnerForwardConfirm(event: unknown, raw: unknown): Promise<OwnerForwardConfirmResult> {
    if (!deps.isTrustedSender(event)) {
      deps.log?.('[owner-forward] confirm refused: not the app main frame')

      return refuse('untrusted_sender', 'owner forward is only available from the app window')
    }

    const req = parseRequest(raw)

    if (typeof req === 'string') {
      return refuse('bad_request', req)
    }

    const slot = gate.tryOpen()

    if ('reason' in slot) {
      const message =
        slot.reason === 'dialog_open'
          ? 'a confirm is already open'
          : slot.reason === 'cooldown'
            ? 'wait a moment before sending again'
            : 'too many confirms this minute'

      return refuse(slot.reason, message)
    }

    let cancelled = false
    const written: string[] = []

    try {
      const backend = deps.backendIdForProfile(req.profile)

      if (!backend) {
        return refuse('no_backend', 'this backend was not started by the app, so it cannot take signed forwards')
      }

      // The origin, looked up by main: its title for the dialog and, for a message id, the stored
      // row's role. The renderer's `origin.role` is never read.
      const source = await deps.resolveSession(req.profile, req.origin.session_id, req.profile, {
        messageId: req.origin.message_id
      })

      if (!source) {
        return refuse('source_missing', `no session ${req.origin.session_id} in profile ${req.profile}`)
      }

      const origin: SignRequest['sourceSession'] = {
        session_id: req.origin.session_id,
        message_id: req.origin.message_id,
        role: deriveSourceRole(req.gesture, req.origin.message_id, source.message_role)
      }

      const targets: SignRequest['targets'] = []
      const claudeStates: Record<string, ClaudeSessionState> = {}

      for (const t of req.targets) {
        const resolved = await deps.resolveSession(t.profile, t.session_id, req.profile)

        if (!resolved) {
          return refuse('target_missing', `no session ${t.session_id} in profile ${t.profile}`)
        }

        const claudeId =
          typeof resolved.claude_session_id === 'string' && resolved.claude_session_id ? resolved.claude_session_id : null

        claudeStates[`${t.profile}:${t.session_id}`] = claudeId ? 'live' : (resolved.claude_session_state ?? 'unknown')
        targets.push({
          profile: t.profile,
          session_id: t.session_id,
          claude_session_id: claudeId,
          backend,
          title: sanitizeDialogTitle(resolved.title) || undefined
        })
      }

      const viewFullText = async (model: ConfirmModel) => {
        if (!deps.fullTextDir || !deps.openPath) {
          throw new FullTextViewError('viewing the full text is not available')
        }

        const file = writeFullTextFile(deps.fullTextDir, fullTextDocument(model), { fs: deps.fs, randomBytes: deps.randomBytes })
        written.push(file)
        const failure = await deps.openPath(file)

        if (failure) {
          throw new FullTextViewError(`could not open the full text: ${failure}`)
        }
      }

      // The single subject the sheet collects binds every scope that requires one (prod).
      const subject: Record<string, string> = {}

      if (req.subject) {
        for (const value of req.scope) {
          try {
            if (parseScope(value).subjectRequired) {
              subject[value] = req.subject
            }
          } catch {
            // an out-of-grammar scope is refused by the signing core below
          }
        }
      }

      const outcome = await confirmAndSignGrants(
        {
          text: req.text,
          gesture: req.gesture,
          sourceSession: origin,
          targets,
          scope: req.scope,
          subject,
          ...(req.ttlMs !== undefined ? { ttlMs: req.ttlMs } : {})
        },
        {
          store: deps.store,
          grantsDir: deps.grantsDir,
          now: deps.now,
          ownerUid: deps.ownerUid,
          touchId: deps.touchId,
          randomBytes: deps.randomBytes,
          fs: deps.fs,
          confirm: async model => {
            const long = isLongText(model.text)
            let viewed = false // per request: a new confirm of the same text must view again
            let checkbox = false

            for (let shown = 0; shown < MAX_DIALOG_SHOWINGS; shown++) {
              const options = buildConfirmDialog(model, deps.formatTime, {
                fullTextViewed: viewed,
                sourceTitle: source.title,
                claudeStates,
                checkboxChecked: checkbox
              })

              const answer = await deps.showMessageBox(options)
              checkbox = answer?.checkboxChecked === true
              const choice = typeof answer?.response === 'number' ? options.buttons[answer.response] : undefined

              if (choice === VIEW_BUTTON) {
                await viewFullText(model)
                viewed = true

                continue
              }

              return { confirmed: choice === SEND_BUTTON && (!long || viewed), acknowledgedUnrecognized: checkbox }
            }

            return { confirmed: false }
          }
        }
      )

      if (outcome.cancelled === true) {
        cancelled = true

        return { ok: false, cancelled: true, reason: (outcome as { reason: 'dialog' | 'touch_id' | 'unrecognized_scope' }).reason }
      }

      const signed = outcome as SignedOutcome
      const grant = signed.grants[0]

      return {
        ok: true,
        decisionId: signed.decisionId,
        grantId: grant.grantId,
        envelope: { ...grant.envelope },
        targets: targets.map(t => `${t.profile}:${t.session_id}`).sort()
      }
    } catch (error) {
      if (error instanceof OwnerGrantSignError) {
        return refuse(error.code, error.message)
      }

      if (error instanceof FullTextViewError) {
        deps.log?.(`[owner-forward] ${error.message}`)

        return refuse('view_failed', error.message)
      }

      const message = error instanceof Error ? error.message : String(error)
      deps.log?.(`[owner-forward] confirm failed: ${message}`)

      return refuse('sign_failed', message)
    } finally {
      // The reading copy never outlives its confirm (the editor keeps what it already loaded).
      for (const file of written) {
        try {
          ;(deps.fs ?? nodeFs).unlinkSync(file)
        } catch {
          // already gone
        }
      }

      gate.close(cancelled)
    }
  }
}

// -- hermes:owner-grant:action ---------------------------------------------------------------------

export interface OwnerGrantActionDeps {
  isTrustedSender: (event: unknown) => boolean
  runAction: (action: unknown) => Promise<unknown>
  now: () => number
  log?: (message: string) => void
}

/** The admin enable / rotate / revoke IPC: the same app-main-frame sender check as the confirm, then
 *  one action at a time, a 3 s cooldown after the owner cancels a prompt, and at most 10 per minute. */
export function createOwnerGrantActionHandler(deps: OwnerGrantActionDeps) {
  const gate = createConfirmRateGate(deps.now)

  return async function handleOwnerGrantAction(event: unknown, action: unknown): Promise<unknown> {
    if (!deps.isTrustedSender(event)) {
      deps.log?.('[owner-grant] action refused: not the app main frame')

      return { ok: false, reason: 'untrusted_sender' }
    }

    const slot = gate.tryOpen()

    if ('reason' in slot) {
      return { ok: false, reason: slot.reason }
    }

    let cancelled = false

    try {
      const result = await deps.runAction(action)
      cancelled = !!result && typeof result === 'object' && (result as { reason?: unknown }).reason === 'cancelled'

      return result
    } finally {
      gate.close(cancelled)
    }
  }
}
