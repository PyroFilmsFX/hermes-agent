/**
 * b10 H9a: the renderer's cached view of main's session → project bindings.
 *
 * Main owns the signed binding record (H7a) and exposes it over
 * `window.hermesDesktop.sessionBinding.{set,clear,status}` (H8). This store only caches the
 * public status per (profile, hermes session id) so the titlebar pill can paint without a round
 * trip, and refreshes it:
 *   - when a session is shown and its entry is missing, failed or older than {@link STATUS_MAX_AGE_MS};
 *   - right after every set / clear this store sends.
 * No polling: main's IPC shares one rate gate across status, set and clear (at most 10 calls a
 * minute, one at a time), so status reads are cache-first and every IPC call is serialized here.
 *
 * The renderer never chooses a project root, seq or nonce: set carries
 * `{profile, hermes_session_id, path}`; main resolves the rest itself. The only root it ever sends
 * back is the one main just resolved (`confirmed_project_root`, after the owner saw it).
 */
import { atom } from 'nanostores'

import type {
  DesktopSessionBindingConfirmRequired,
  DesktopSessionBindingOutcome,
  DesktopSessionBindingRecord,
  DesktopSessionBindingSessionInput,
  DesktopSessionBindingSetInput
} from '@/global'
import { pathLeaf } from '@/lib/display-path'
import { activeGateway } from '@/store/gateway'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import type { ProjectInfo } from '@/types/hermes'

export const STATUS_MAX_AGE_MS = 60_000

export type SessionBindingEntry =
  | { status: 'loading'; record: DesktopSessionBindingRecord | null; at: number }
  | { status: 'ready'; record: DesktopSessionBindingRecord; at: number }
  | { status: 'error'; record: DesktopSessionBindingRecord | null; reason: string; at: number }

/** The workspace the pill offers first: the session's own cwd, named from its repo / project. */
export interface SessionBindingSuggestion {
  /** Sent to main as `path`; main resolves the worktree toplevel itself (owner decision O3). */
  path: string
  /** Repo or project name shown in "Bind to <repo>?". */
  name: string
  branch: null | string
}

export const $sessionBindings = atom<Record<string, SessionBindingEntry>>({})
export const $sessionBindingSuggestions = atom<Record<string, SessionBindingSuggestion | null>>({})

export function sessionBindingKey(profile: null | string | undefined, sessionId: string): string {
  return `${normalizeProfileKey(profile)}\u0000${sessionId}`
}

function bridge() {
  return typeof window === 'undefined' ? undefined : window.hermesDesktop?.sessionBinding
}

/** True when main exposes the binding IPC (the desktop app, not a plain browser build). */
export function sessionBindingAvailable(): boolean {
  return Boolean(bridge())
}

// One IPC call at a time: main's gate refuses an overlapping call with `dialog_open`.
let queue: Promise<unknown> = Promise.resolve()

function serialized<T>(run: () => Promise<T>): Promise<T> {
  const next = queue.then(run, run)

  queue = next.catch(() => undefined)

  return next
}

const inFlight = new Map<string, Promise<void>>()

function writeEntry(key: string, entry: SessionBindingEntry): void {
  $sessionBindings.set({ ...$sessionBindings.get(), [key]: entry })
}

function refusalReason(outcome: { reason?: unknown } | null | undefined): string {
  return typeof outcome?.reason === 'string' && outcome.reason ? outcome.reason : 'unavailable'
}

/** Re-read main's status for one session (serialized, deduped per key). */
export function refreshSessionBinding(profile: null | string | undefined, sessionId: string): Promise<void> {
  const status = bridge()?.status
  const key = sessionBindingKey(profile, sessionId)

  if (!status || !sessionId) {
    return Promise.resolve()
  }

  const pending = inFlight.get(key)

  if (pending) {
    return pending
  }

  const previous = $sessionBindings.get()[key]
  const lastRecord = previous?.record ?? null

  writeEntry(key, { status: 'loading', record: lastRecord, at: previous?.at ?? 0 })

  const input: DesktopSessionBindingSessionInput = {
    profile: normalizeProfileKey(profile),
    hermes_session_id: sessionId
  }

  const run = serialized(() => status(input))
    .then(result => {
      if (result && result.ok) {
        writeEntry(key, { status: 'ready', record: result, at: Date.now() })
      } else {
        // A refused read (rate gate, untrusted sender) keeps the last good record visible.
        writeEntry(key, { status: 'error', record: lastRecord, reason: refusalReason(result), at: Date.now() })
      }
    })
    .catch(error => {
      writeEntry(key, {
        status: 'error',
        record: lastRecord,
        reason: error instanceof Error ? error.message : 'unavailable',
        at: Date.now()
      })
    })
    .finally(() => {
      inFlight.delete(key)
    })

  inFlight.set(key, run)

  return run
}

/** A read that started before a write may carry the old record: let it land, then read again. */
async function refreshAfterWrite(profile: string, sessionId: string): Promise<void> {
  await inFlight.get(sessionBindingKey(profile, sessionId))
  await refreshSessionBinding(profile, sessionId)
}

/** Cache-first read: fetch only when the entry is missing, failed or stale. */
export function ensureSessionBinding(profile: null | string | undefined, sessionId: string, now = Date.now()): void {
  if (!sessionId || !bridge()) {
    return
  }

  const entry = $sessionBindings.get()[sessionBindingKey(profile, sessionId)]

  if (entry && entry.status === 'loading') {
    return
  }

  if (entry && entry.status === 'ready' && now - entry.at < STATUS_MAX_AGE_MS) {
    return
  }

  void refreshSessionBinding(profile, sessionId)
}

/** The root main resolved for a bind (step one), shown in the owner's confirm before it is signed. */
export type SessionBindingResolvedRoot = Pick<
  DesktopSessionBindingConfirmRequired,
  'current_project_root' | 'project_root' | 'repo_common_root' | 'repo_remote'
>

export interface SetSessionBindingOptions {
  /** The owner's confirm, shown the exact root main will sign. Resolve false to sign nothing. */
  confirmRoot?: (resolved: SessionBindingResolvedRoot) => Promise<boolean>
}

function isConfirmRequired(outcome: unknown): outcome is DesktopSessionBindingConfirmRequired {
  const o = outcome as null | Partial<DesktopSessionBindingConfirmRequired> | undefined

  return Boolean(o && o.ok === false && o.reason === 'confirm_required' && typeof o.project_root === 'string')
}

/**
 * Owner-clicked bind, two-step (b10 review): send `{profile, hermes_session_id, path}` and main
 * resolves the root it would sign without signing; `confirmRoot` shows that exact root; the commit
 * sends it back as `confirmed_project_root`, and main refuses if a fresh probe resolves elsewhere.
 * Then re-reads status.
 */
export async function setSessionBinding(
  input: DesktopSessionBindingSetInput,
  options: SetSessionBindingOptions = {}
): Promise<DesktopSessionBindingOutcome> {
  const set = bridge()?.set

  if (!set) {
    return { ok: false, reason: 'unavailable' }
  }

  const params: DesktopSessionBindingSetInput = {
    profile: normalizeProfileKey(input.profile),
    hermes_session_id: input.hermes_session_id,
    path: input.path
  }

  const probe = await serialized(() => set(params))

  if (!isConfirmRequired(probe)) {
    await refreshAfterWrite(params.profile, params.hermes_session_id)

    return probe
  }

  const resolved: SessionBindingResolvedRoot = {
    current_project_root: probe.current_project_root ?? null,
    project_root: probe.project_root,
    repo_common_root: probe.repo_common_root ?? null,
    repo_remote: probe.repo_remote ?? null
  }

  if (options.confirmRoot && !(await options.confirmRoot(resolved))) {
    return { ok: false, reason: 'cancelled' }
  }

  const outcome = await serialized(() => set({ ...params, confirmed_project_root: resolved.project_root }))

  await refreshAfterWrite(params.profile, params.hermes_session_id)

  return outcome
}

/** Owner-clicked unbind, then re-reads status. */
export async function clearSessionBinding(
  input: DesktopSessionBindingSessionInput
): Promise<DesktopSessionBindingOutcome> {
  const clear = bridge()?.clear

  if (!clear) {
    return { ok: false, reason: 'unavailable' }
  }

  const params: DesktopSessionBindingSessionInput = {
    profile: normalizeProfileKey(input.profile),
    hermes_session_id: input.hermes_session_id
  }

  const outcome = await serialized(() => clear(params))

  await refreshAfterWrite(params.profile, params.hermes_session_id)

  return outcome
}

// ── Suggestion ────────────────────────────────────────────────────────────────

interface ProjectForCwdPayload {
  project: null | ProjectInfo
  cwd?: string
  branch?: null | string
}

/** `projects.for_cwd` on the live gateway, only when it serves the session's profile. */
async function projectForCwd(profile: string, cwd: string): Promise<ProjectForCwdPayload | null> {
  const gateway = activeGateway()

  if (!gateway || gateway.connectionState !== 'open' || normalizeProfileKey($activeGatewayProfile.get()) !== profile) {
    return null
  }

  try {
    return await gateway.request<ProjectForCwdPayload>('projects.for_cwd', { cwd, profile })
  } catch {
    return null
  }
}

export interface SuggestionSource {
  cwd?: null | string
  git_branch?: null | string
  git_repo_root?: null | string
  id: string
  profile?: null | string
}

/** The workspace a session suggests, from its recorded git repo / cwd alone (no I/O). */
export function suggestionFromSession(session: SuggestionSource): SessionBindingSuggestion | null {
  const cwd = (session.cwd ?? '').trim()
  const repoRoot = (session.git_repo_root ?? '').trim()
  const path = cwd || repoRoot

  // A non-git cwd can't be bound (main requires a git toplevel), so it isn't suggested.
  if (!path || !repoRoot) {
    return null
  }

  return { path, name: pathLeaf(repoRoot) || pathLeaf(path), branch: session.git_branch?.trim() || null }
}

const suggestionsInFlight = new Set<string>()

/**
 * Resolve the session's suggested workspace once per session: the recorded repo first, then
 * `projects.for_cwd` for a project name (and to recognise a git cwd the backfill hasn't reached).
 */
export function ensureSessionBindingSuggestion(session: SuggestionSource): void {
  const profile = normalizeProfileKey(session.profile)
  const key = sessionBindingKey(profile, session.id)

  if (!session.id || key in $sessionBindingSuggestions.get() || suggestionsInFlight.has(key)) {
    return
  }

  const local = suggestionFromSession(session)
  const cwd = (session.cwd ?? '').trim()

  // A row that hasn't reported its workspace yet stays unresolved, so a later cwd still suggests.
  if (!local && !cwd) {
    return
  }

  $sessionBindingSuggestions.set({ ...$sessionBindingSuggestions.get(), [key]: local })

  if (!cwd) {
    return
  }

  suggestionsInFlight.add(key)

  void projectForCwd(profile, cwd)
    .then(payload => {
      if (!payload) {
        return
      }

      const branch = payload.branch?.trim() || local?.branch || null
      const projectName = payload.project?.name?.trim() || ''

      // Neither a recorded repo, a project nor a branch: not a workspace we can bind.
      if (!local && !projectName && !branch) {
        return
      }

      $sessionBindingSuggestions.set({
        ...$sessionBindingSuggestions.get(),
        [key]: { path: cwd, name: projectName || local?.name || pathLeaf(cwd), branch }
      })
    })
    .finally(() => {
      suggestionsInFlight.delete(key)
    })
}

export function resetSessionBindingsForTests(): void {
  queue = Promise.resolve()
  inFlight.clear()
  suggestionsInFlight.clear()
  $sessionBindings.set({})
  $sessionBindingSuggestions.set({})
}
