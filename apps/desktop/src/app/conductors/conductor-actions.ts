// Row actions for the Conductors pane (#49 R6): open a build's owner session
// with the owner's intent, stash a message into its composer, copy ids.
//
// Kept out of the row component so the pane can swap them in tests and so the
// row stays a pure view of its data.

import type { ConductorRow } from '@/api/conductors'
import { requestComposerFocus } from '@/app/chat/composer/focus'
import {
  openSession,
  type OpenSessionIntent,
  openSessionIntentFromModifiers,
  type OpenSessionNavigate
} from '@/app/open-session'
import { openExternalLink } from '@/lib/external-link'
import { requestComposerDraftSync, stashSessionDraft, takeSessionDraft } from '@/store/composer'
import { normalizeProfileKey } from '@/store/profile'
import { $selectedStoredSessionId, $sessions, lineageAliases, resolveComposerSessionKey } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

/** Modifier state of the gesture that asked to open (click or key). */
export type ConductorOpenEvent = null | undefined | { ctrlKey?: boolean; metaKey?: boolean; shiftKey?: boolean }

/** Where a row opens: its Hermes session, and the profile that owns it when
 *  that is not the profile this window is on. */
export interface ConductorTarget {
  ownerProfile?: string
  sessionId: string
}

/** Null for an unattributed row (a terminal or remote Claude session). */
export function conductorTarget(row: ConductorRow, activeProfile: string): ConductorTarget | null {
  const sessionId = row.orchestrator.hermes_session_id?.trim()

  if (!sessionId) {
    return null
  }

  const profile = row.orchestrator.profile?.trim()

  if (profile && normalizeProfileKey(profile) !== normalizeProfileKey(activeProfile)) {
    return { ownerProfile: normalizeProfileKey(profile), sessionId }
  }

  return { sessionId }
}

/**
 * §8: plain click stacks (never steals a busy main), ⌘ opens a tab, ⇧⌘ a
 * window. A cross-profile row can't use main — main belongs to this window's
 * profile and carries no owner — so its plain click takes a tab, which does.
 */
export function conductorOpenIntent(event: ConductorOpenEvent, target: ConductorTarget): OpenSessionIntent {
  const intent = openSessionIntentFromModifiers(event, 'stack')

  return target.ownerProfile && intent === 'stack' ? 'tab' : intent
}

export function openConductorTarget(
  target: ConductorTarget,
  intent: OpenSessionIntent,
  navigate: OpenSessionNavigate
): void {
  openSession(
    target.sessionId,
    navigate,
    intent,
    target.ownerProfile ? { ownerProfile: target.ownerProfile, workspaceMode: 'sessions' } : undefined
  )
}

/** The composer bus address showing `sessionId` right now, if any. */
function composerTargetFor(sessionId: string): null | string {
  const aliases = lineageAliases(sessionId, $sessions.get())
  const tile = $sessionTiles.get().find(candidate => aliases.includes(candidate.storedSessionId))

  if (tile) {
    return `tile:${tile.storedSessionId}`
  }

  return aliases.includes($selectedStoredSessionId.get() ?? '') ? 'main' : null
}

/**
 * Send… (§8): the owner typing in that session, nothing more. The text goes
 * into the session's composer draft — after anything already drafted there,
 * never over it — and the session opens as a tab with its composer focused.
 * Nothing is submitted: the owner presses Enter in the session. Returns false
 * (and does nothing) for blank text.
 */
export function sendToConductorTarget(target: ConductorTarget, text: string, navigate: OpenSessionNavigate): boolean {
  const body = text.trim()

  if (!body) {
    return false
  }

  const draftKey = resolveComposerSessionKey(target.sessionId, $sessions.get()) ?? target.sessionId
  const existing = takeSessionDraft(draftKey)
  const merged = existing.text.trim() ? `${existing.text.replace(/\s+$/, '')}\n\n${body}` : body

  stashSessionDraft(draftKey, merged, existing.attachments)
  openConductorTarget(target, 'tab', navigate)

  // A composer that was already mounted on this session read its draft long
  // ago: have it re-read the stash. A freshly opened tab reads it on mount.
  const composer = composerTargetFor(target.sessionId)

  if (composer) {
    requestComposerDraftSync('reload', composer)
    requestComposerFocus(composer)
  }

  return true
}

export function copyConductorText(text: null | string | undefined): void {
  if (!text) {
    return
  }

  void navigator.clipboard?.writeText(text).catch(() => undefined)
}

/** Only GitHub links leave the app from this page (§8 overflow). */
export function githubUrlOf(url: null | string | undefined): null | string {
  return url && url.startsWith('https://github.com/') ? url : null
}

export function openConductorExternal(url: null | string | undefined): void {
  const safe = githubUrlOf(url)

  if (safe) {
    openExternalLink(safe)
  }
}
