/**
 * #60 owner-forward client (design §4, §5.4): the Forward sheet state, the target candidates, and the
 * one send path: main's native confirm (`window.hermesDesktop.ownerForward.confirm`) → the gateway's
 * `owner.forward` with the signed envelope → per-target status.
 *
 * Who may start a send: the sheet's Send… (trusted click), a typed `/to` in `submitDraft`, and ⌘⇧↩
 * (trusted key). Proposal cards and "Forward to…" only OPEN the sheet, and only on a trusted gesture.
 * Titles are display only here; main resolves the titles the owner confirms.
 */
import { atom } from 'nanostores'

import type { DesktopOwnerForwardGesture } from '@/global'
import { $gateway } from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'
import { $sessions } from '@/store/session'

import type { ForwardCandidate } from './parse-to'
import { scopeTtlPolicy } from './scopes'
import { isTrustedGesture } from './trusted'

export const FORWARD_MAX_CHARS = 8000
const FORWARD_TIMEOUT_MS = 180_000

export type ForwardGesture = DesktopOwnerForwardGesture

export interface ForwardOrigin {
  session_id: string
  message_id: null | string
  role: 'assistant' | 'peer' | 'user' | null
}

export interface ForwardTarget {
  profile: string
  session_id: string
  title?: null | string
}

export interface ForwardResultLine {
  target: string
  status: string
  detail: null | string
}

export type ForwardOutcome =
  | { kind: 'cancelled' }
  | { kind: 'error'; message: string; code?: string }
  | { kind: 'sent'; decisionId: string; results: ForwardResultLine[] }

export interface ForwardSheetState {
  text: string
  gesture: ForwardGesture
  origin: ForwardOrigin
  targets: ForwardTarget[]
  scope: string[]
  ttlMs: number
  subject: string
  proposalId?: string
}

export interface ForwardSheetInit {
  text: string
  gesture: ForwardGesture
  origin: ForwardOrigin
  targets?: ForwardTarget[]
  scope?: string[]
  ttlMs?: number
  subject?: null | string
  proposalId?: string
}

export const $forwardSheet = atom<ForwardSheetState | null>(null)

/** Settled outcomes by proposal id, so a proposal card goes inert after its forward. */
export const $forwardReceipts = atom<Record<string, ForwardOutcome>>({})

function sheetState(init: ForwardSheetInit): ForwardSheetState {
  const scope = [...(init.scope ?? [])]
  const ttlPolicy = scopeTtlPolicy(scope)

  return {
    text: init.text,
    gesture: init.gesture,
    origin: { ...init.origin },
    targets: (init.targets ?? []).slice(0, 5).map(t => ({ ...t })),
    scope,
    ttlMs: Math.min(init.ttlMs ?? ttlPolicy.defaultTtlMs, ttlPolicy.maxTtlMs),
    subject: init.subject ?? '',
    ...(init.proposalId ? { proposalId: init.proposalId } : {})
  }
}

/** Open the Forward sheet from a trusted click / key only (menus, proposal cards). */
export function openForwardSheet(init: ForwardSheetInit, event: { isTrusted?: boolean } | null | undefined): boolean {
  if (!isTrustedGesture(event)) {
    return false
  }

  $forwardSheet.set(sheetState(init))

  return true
}

/** Open the sheet from the composer's typed-draft path (an ambiguous or unknown `/to` target).
 *  Only `submitDraft` calls this; nothing a widget can reach does. */
export function openForwardSheetFromTypedDraft(init: ForwardSheetInit): void {
  $forwardSheet.set(sheetState(init))
}

export function closeForwardSheet(): void {
  $forwardSheet.set(null)
}

export function forwardProfile(): string {
  return $activeGatewayProfile.get() || 'default'
}

/** Forward targets the owner can pick: stored chats on this backend, minus the origin. Pinned first,
 *  then most recent. */
export function forwardCandidates(originSessionId: null | string): ForwardCandidate[] {
  const profile = forwardProfile()

  return $sessions
    .get()
    .filter(s => s.id !== originSessionId && !s.archived && !s.hidden)
    .sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)) || (b.last_active ?? 0) - (a.last_active ?? 0))
    .map(s => ({ profile: s.profile || profile, session_id: s.id, title: s.title }))
}

type GatewayRequest = (method: string, params: Record<string, unknown>, timeoutMs?: number) => Promise<unknown>

/** One console line per failed forward (live RCA 2026-09-28: a refusal left no trace anywhere).
 *  Codes only: never the text or the envelope. */
function forwardFailed(stage: 'confirm' | 'deliver' | 'setup', code: string, message: string): ForwardOutcome {
  console.warn(`[owner-forward] ${stage} failed: ${code}`)

  return { kind: 'error', message, code }
}

function errorCode(error: unknown): string {
  const code = (error as { code?: unknown } | null)?.code

  return typeof code === 'string' || typeof code === 'number' ? String(code) : 'error'
}

let gatewayOverride: GatewayRequest | null = null

export function setForwardGatewayRequestForTests(request: GatewayRequest | null): void {
  gatewayOverride = request
}

function gatewayRequest(): GatewayRequest | null {
  if (gatewayOverride) {
    return gatewayOverride
  }

  const gateway = $gateway.get() as { request?: GatewayRequest } | null

  return gateway?.request ? (method, params, timeoutMs) => gateway.request!(method, params, timeoutMs) : null
}

export interface SendForwardInput {
  text: string
  gesture: ForwardGesture
  origin: ForwardOrigin
  targets: ForwardTarget[]
  scope?: string[]
  ttlMs?: number
  subject?: null | string
  proposalId?: string
}

function titleFor(id: string, targets: ForwardTarget[]): string {
  const bare = id.includes(':') ? id.slice(id.indexOf(':') + 1) : id
  const hit = targets.find(t => t.session_id === bare)

  return hit?.title || bare
}

/**
 * Confirm in main (native dialog), then deliver through `owner.forward`. `onConfirmed` runs once main
 * returns a signed envelope (the owner pressed Send), before delivery: callers clear drafts there.
 */
export async function sendOwnerForward(
  input: SendForwardInput,
  hooks: { onConfirmed?: () => void } = {}
): Promise<ForwardOutcome> {
  const bridge = window.hermesDesktop?.ownerForward

  if (!bridge) {
    return forwardFailed('setup', 'no_bridge', 'Forwarding needs the desktop app.')
  }

  const subject = input.subject?.trim()
  const request = gatewayRequest()

  if (!request) {
    return forwardFailed('setup', 'no_gateway', 'Hermes gateway unavailable')
  }

  let textToSign = input.text

  try {
    const masked = (await request('secrets.mask', {
      text: input.text,
      session_id: input.origin.session_id || null
    })) as { text?: string } | null

    if (typeof masked?.text === 'string') {
      textToSign = masked.text
    }
  } catch (error) {
    // If secrets.mask fails or is unavailable, keep textToSign as input.text
    console.warn(`[owner-forward] secrets.mask failed: ${errorCode(error)}; signing the text as shown`)
  }

  let confirmed: Awaited<ReturnType<typeof bridge.confirm>>

  try {
    confirmed = await bridge.confirm({
      text: textToSign,
      gesture: input.gesture,
      origin: { ...input.origin },
      profile: forwardProfile(),
      // Ids only: main resolves the titles the owner sees.
      targets: input.targets.map(t => ({ profile: t.profile, session_id: t.session_id })),
      scope: [...(input.scope ?? [])],
      ...(input.ttlMs !== undefined ? { ttlMs: input.ttlMs } : {}),
      ...(subject ? { subject } : {})
    })
  } catch (error) {
    return forwardFailed('confirm', 'ipc_error', error instanceof Error ? error.message : String(error))
  }

  if (!confirmed || typeof confirmed !== 'object') {
    return forwardFailed('confirm', 'no_answer', 'The app did not answer the confirm request.')
  }

  if (!confirmed.ok) {
    if (confirmed.cancelled) {
      return { kind: 'cancelled' }
    }

    return forwardFailed('confirm', confirmed.code || 'refused', confirmed.error || 'The forward was refused.')
  }

  hooks.onConfirmed?.()

  let outcome: ForwardOutcome

  try {
    const response = (await request(
      'owner.forward',
      { envelope: confirmed.envelope, text: textToSign, targets: confirmed.targets },
      FORWARD_TIMEOUT_MS
    )) as { results?: Array<{ target_session_id?: string; status?: string; detail?: null | string }> } | null

    const results = (response?.results ?? []).map(row => ({
      target: titleFor(String(row.target_session_id ?? ''), input.targets),
      status: String(row.status ?? 'failed:submit'),
      detail: row.detail ?? null
    }))

    outcome = { kind: 'sent', decisionId: confirmed.decisionId, results }
  } catch (error) {
    outcome = forwardFailed('deliver', errorCode(error), error instanceof Error ? error.message : String(error))
  }

  if (input.proposalId) {
    $forwardReceipts.set({ ...$forwardReceipts.get(), [input.proposalId]: outcome })
  }

  return outcome
}

export interface ForwardStatusCopy {
  statusDelivered: (target: string) => string
  statusQueued: (target: string) => string
  statusResumed: (target: string) => string
  statusFailed: (target: string, detail: string) => string
}

export function describeForwardResult(line: ForwardResultLine, copy: ForwardStatusCopy): string {
  if (line.status === 'delivered') {
    return copy.statusDelivered(line.target)
  }

  if (line.status === 'queued') {
    return copy.statusQueued(line.target)
  }

  if (line.status === 'resumed-and-delivered') {
    return copy.statusResumed(line.target)
  }

  return copy.statusFailed(line.target, line.detail || line.status.replace(/^failed:/, '').replace(/_/g, ' '))
}
