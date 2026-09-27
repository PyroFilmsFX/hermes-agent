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
 * 10 confirms per rolling minute.
 */

import {
  confirmAndSignGrants,
  type ConfirmModel,
  GESTURES,
  type Gesture,
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
const MINUTE_MS = 60_000

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
  formatTime: (ms: number) => string = ms => new Date(ms).toLocaleString()
): ConfirmDialogOptions {
  const lines: string[] = []
  lines.push(model.targets.length === 1 ? 'To:' : `To (${model.targets.length}):`)

  for (const t of model.targets) {
    lines.push(`  • ${t.title || 'Untitled session'} (${t.profile}, ${shortId(t.session_id)})`)
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

  if (model.gesture) {
    lines.push('')
    lines.push(`From: ${GESTURE_LABEL[model.gesture] ?? model.gesture}`)
  }

  const prod = model.scopes.some(s => s.scopeClass === 'prod')

  return {
    type: prod || model.requiresUnrecognizedAck ? 'warning' : 'question',
    title: 'Send as you',
    message: 'Send as you?',
    detail: lines.join('\n'),
    buttons: ['Send', 'Cancel'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
    ...(model.requiresUnrecognizedAck
      ? { checkboxLabel: 'Sign the unrecognized scope listed above', checkboxChecked: false }
      : {})
  }
}

// -- handler -----------------------------------------------------------------------------------

export interface ResolvedSession {
  title: string | null
  claude_session_id: string | null
}

export interface OwnerForwardConfirmDeps {
  isTrustedSender: (event: unknown) => boolean
  /** The `HERMES_OWNER_GRANT_BACKEND` main handed the backend serving this profile, or null. */
  backendIdForProfile: (profile: string) => string | null
  /** Main's own lookup (`/api/sessions/{id}` with main's token, through the delivering backend's
   *  profile). null = no such session. */
  resolveSession: (profile: string, sessionId: string, backendProfile: string) => Promise<ResolvedSession | null>
  showMessageBox: (options: ConfirmDialogOptions) => Promise<{ response: number; checkboxChecked?: boolean }>
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

    try {
      const backend = deps.backendIdForProfile(req.profile)

      if (!backend) {
        return refuse('no_backend', 'this backend was not started by the app, so it cannot take signed forwards')
      }

      const targets: SignRequest['targets'] = []

      for (const t of req.targets) {
        const resolved = await deps.resolveSession(t.profile, t.session_id, req.profile)

        if (!resolved) {
          return refuse('target_missing', `no session ${t.session_id} in profile ${t.profile}`)
        }

        targets.push({
          profile: t.profile,
          session_id: t.session_id,
          claude_session_id: resolved.claude_session_id,
          backend,
          title: resolved.title ?? undefined
        })
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
          sourceSession: req.origin,
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
            const answer = await deps.showMessageBox(buildConfirmDialog(model, deps.formatTime))

            return { confirmed: answer?.response === 0, acknowledgedUnrecognized: answer?.checkboxChecked === true }
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

      const message = error instanceof Error ? error.message : String(error)
      deps.log?.(`[owner-forward] confirm failed: ${message}`)

      return refuse('sign_failed', message)
    } finally {
      gate.close(cancelled)
    }
  }
}
