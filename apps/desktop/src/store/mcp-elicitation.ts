import { registryBackendScopeKey } from '@hermes/shared'
import { atom } from 'nanostores'

import { translateNow } from '@/i18n'
import { $gateway, activeGatewayConnectionId, requestGatewayForAgent } from '@/store/gateway'
import { notify } from '@/store/notifications'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'

/**
 * MCP elicitation requests (M4a `mcp.elicitation.request`) waiting on the user.
 *
 * The gateway broadcasts the request session-less; the backend blocks the MCP
 * tool call until `mcp.elicitation.respond` lands or its own timeout (config
 * `mcp.elicitation_timeout_seconds`, default 300 s) answers `decline`. The
 * backend sends NOTHING when it times out, so each card carries a client-side
 * deadline of the same default and is dropped silently when it passes —
 * never answered, and never accepted.
 *
 * Requests queue in arrival order; the host shows one at a time.
 */

/** Backend default for `mcp.elicitation_timeout_seconds`. */
export const MCP_ELICITATION_TIMEOUT_MS = 300_000

export type McpElicitationAction = 'accept' | 'cancel' | 'decline'

interface McpElicitationBase {
  requestId: string
  server: string
  message: string
  /** Session the request belongs to, when the gateway names one. `null` =
   *  app-level (the M4a gateway broadcasts elicitation session-less). */
  sessionId: null | string
  connectionId: null | string
  profile: null | string
  receivedAt: number
}

export type McpElicitationRequest =
  | (McpElicitationBase & { mode: 'form'; requestedSchema: Record<string, unknown> })
  | (McpElicitationBase & { mode: 'url'; url: string })

export const $mcpElicitations = atom<McpElicitationRequest[]>([])

const timers = new Map<string, ReturnType<typeof setTimeout>>()

const clearTimer = (requestId: string) => {
  const timer = timers.get(requestId)

  if (timer !== undefined) {
    clearTimeout(timer)
    timers.delete(requestId)
  }
}

const str = (value: unknown): string => (typeof value === 'string' ? value : '')

/** Normalise a raw event payload; `null` for anything malformed. */
export function normalizeMcpElicitation(
  payload: unknown,
  source: { connectionId?: null | string; profile?: null | string; sessionId?: null | string } = {},
  receivedAt = Date.now()
): McpElicitationRequest | null {
  if (!payload || typeof payload !== 'object') {
    return null
  }

  const raw = payload as Record<string, unknown>
  const requestId = str(raw.request_id).trim()

  if (!requestId) {
    return null
  }

  const base: McpElicitationBase = {
    requestId,
    server: str(raw.server).trim() || 'MCP server',
    message: str(raw.message),
    sessionId: source.sessionId || null,
    connectionId: source.connectionId || null,
    profile: source.profile || null,
    receivedAt
  }

  if (raw.mode === 'url') {
    const url = str(raw.url).trim()

    return url ? { ...base, mode: 'url', url } : null
  }

  if (raw.mode !== undefined && raw.mode !== null && raw.mode !== 'form') {
    return null
  }

  const schema = raw.requestedSchema

  return {
    ...base,
    mode: 'form',
    requestedSchema:
      schema && typeof schema === 'object' && !Array.isArray(schema) ? (schema as Record<string, unknown>) : {}
  }
}

/** Remove a request (answered, expired or withdrawn). */
export function settleMcpElicitation(requestId: string): void {
  clearTimer(requestId)

  const queue = $mcpElicitations.get()

  if (queue.some(request => request.requestId === requestId)) {
    $mcpElicitations.set(queue.filter(request => request.requestId !== requestId))
  }
}

/** Client-side deadline passed: the backend already declined on its side.
 *  Drop the card WITHOUT answering — no RPC, so nothing can be accepted. */
function expireMcpElicitation(requestId: string): void {
  const request = $mcpElicitations.get().find(entry => entry.requestId === requestId)

  settleMcpElicitation(requestId)

  if (request) {
    notify({ kind: 'info', message: translateNow('prompts.mcpElicitation.timedOut', request.server) })
  }
}

/** Queue a request (idempotent per request id) and arm its deadline. */
export function receiveMcpElicitation(request: McpElicitationRequest, timeoutMs = MCP_ELICITATION_TIMEOUT_MS): void {
  const queue = $mcpElicitations.get()

  if (queue.some(entry => entry.requestId === request.requestId)) {
    return
  }

  $mcpElicitations.set([...queue, request])
  clearTimer(request.requestId)
  timers.set(
    request.requestId,
    setTimeout(() => expireMcpElicitation(request.requestId), Math.max(0, timeoutMs))
  )
}

/** The request the host should show now: the oldest that is app-level,
 *  belongs to the visible session, or was explicitly revealed. */
export function visibleMcpElicitation(
  queue: readonly McpElicitationRequest[],
  activeSessionId: null | string,
  revealedId: null | string = null
): McpElicitationRequest | null {
  return (
    queue.find(
      request => request.sessionId === null || request.sessionId === activeSessionId || request.requestId === revealedId
    ) ?? null
  )
}

function fromActiveSource(request: McpElicitationRequest): boolean {
  if (!request.profile) {
    return true
  }

  return (
    normalizeProfileKey(request.profile) === normalizeProfileKey($activeGatewayProfile.get()) &&
    registryBackendScopeKey(request.connectionId, request.profile) ===
      registryBackendScopeKey(activeGatewayConnectionId(), request.profile)
  )
}

/** True when the backend no longer holds the request (answered elsewhere,
 *  timed out, or the server shut down): gateway error 4018. */
export function isExpiredMcpElicitation(error: unknown): boolean {
  const code = error && typeof error === 'object' ? (error as { code?: unknown }).code : undefined

  if (code === 4018) {
    return true
  }

  const message = error instanceof Error ? error.message : String(error)

  return /unknown or expired elicitation request/i.test(message)
}

/**
 * Answer a request on the gateway that raised it. `content` is sent only with
 * `accept`. URL-mode requests support `accept` / `decline` only (the backend
 * rejects `cancel`), so a URL-mode `cancel` is sent as `decline`.
 */
export async function respondMcpElicitation(
  request: McpElicitationRequest,
  action: McpElicitationAction,
  content?: Record<string, unknown>
): Promise<void> {
  const wireAction: McpElicitationAction = request.mode === 'url' && action === 'cancel' ? 'decline' : action

  const params: Record<string, unknown> = {
    request_id: request.requestId,
    action: wireAction,
    ...(wireAction === 'accept' && request.mode === 'form' ? { content: content ?? {} } : {})
  }

  if (fromActiveSource(request)) {
    const gateway = $gateway.get()

    if (!gateway) {
      throw new Error(translateNow('prompts.gatewayDisconnected'))
    }

    await gateway.request('mcp.elicitation.respond', params)
  } else {
    await requestGatewayForAgent(request.connectionId, request.profile ?? 'default', 'mcp.elicitation.respond', params)
  }
}

export function resetMcpElicitationsForTests(): void {
  for (const timer of timers.values()) {
    clearTimeout(timer)
  }

  timers.clear()
  $mcpElicitations.set([])
}
