/**
 * MCP Apps (unit M9): find the `ui://` app a tool result points at, load its
 * HTML through the M6a `mcp.resources.read` RPC, and execute app-initiated
 * tool calls through the gateway (where the M2 trust gate runs again).
 */

import { isMissingRpcMethod } from '@/lib/gateway-rpc'
import { resolveSessionOwner } from '@/app/session/hooks/use-session-actions/utils'
import { requestConnectedGatewayForOwner } from '@/store/gateway'
import { isSessionOwnerRoute } from '@/store/session-request-router'

import type { McpAppToolRequest, McpAppToolResult } from './bridge'

const UI_URI = /^ui:\/\/[^\s"'<>\\]{1,512}$/

export const isMcpAppUri = (value: unknown): value is string => typeof value === 'string' && UI_URI.test(value)

const recordOf = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null

function resultRecord(result: unknown): Record<string, unknown> | null {
  if (typeof result === 'string') {
    const text = result.trim()

    if (!text.startsWith('{')) {
      return null
    }

    try {
      return recordOf(JSON.parse(text))
    } catch {
      return null
    }
  }

  return recordOf(result)
}

function uriFromMeta(meta: Record<string, unknown> | null): string | null {
  if (!meta) {
    return null
  }

  const ui = recordOf(meta.ui)
  const candidates = [ui?.resourceUri, meta['ui/resourceUri']]

  return candidates.find(isMcpAppUri) ?? null
}

function uriFromContent(content: unknown): string | null {
  if (!Array.isArray(content)) {
    return null
  }

  for (const block of content) {
    const rec = recordOf(block)

    if (!rec) {
      continue
    }

    if (rec.type === 'resource_link' && isMcpAppUri(rec.uri)) {
      return rec.uri
    }

    const resource = recordOf(rec.resource)

    if (rec.type === 'resource' && isMcpAppUri(resource?.uri)) {
      return resource.uri as string
    }
  }

  return null
}

/**
 * The `ui://` app a completed tool result references, or null. Recognises the
 * MCP Apps `_meta.ui.resourceUri` (and the older flat `ui/resourceUri` key)
 * and `resource_link` / embedded `resource` content blocks.
 */
export function findMcpAppUri(result: unknown): string | null {
  const rec = resultRecord(result)

  if (!rec) {
    return null
  }

  return (
    uriFromMeta(recordOf(rec._meta)) ?? uriFromContent(rec.content) ?? uriFromContent(recordOf(rec.result)?.content)
  )
}

/** `mcp__<server>__<tool>` or `<server>/<tool>` → the (sanitized) server name. */
export function mcpServerFromToolName(toolName: string): string | null {
  if (toolName.startsWith('mcp__')) {
    const rest = toolName.slice(5)
    const sep = rest.indexOf('__')

    return sep > 0 ? rest.slice(0, sep) : null
  }

  const slash = toolName.indexOf('/')

  return slash > 0 ? toolName.slice(0, slash) : null
}

/** Human label for an app from its URI: `ui://fixture/app` → `fixture/app`. */
export const mcpAppLabel = (uri: string) => uri.replace(/^ui:\/\//, '') || uri

export interface McpResourceReadResult {
  server: string
  uri: string
  contents: { uri: string; mimeType?: string | null; text?: string | null; blob?: string | null }[]
}

export type McpAppHtml = { ok: true; html: string; server: string } | { ok: false; reason: 'not_html' }

function decodeBase64Utf8(blob: string): string | null {
  try {
    const binary = atob(blob)
    const bytes = Uint8Array.from(binary, ch => ch.charCodeAt(0))

    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    return null
  }
}

/** Pick the HTML document out of a resource read (`text/html`, incl. `;profile=mcp-app`). */
export function pickMcpAppHtml(read: McpResourceReadResult): McpAppHtml {
  for (const block of read.contents ?? []) {
    const mime = (block.mimeType ?? '').toLowerCase()

    if (!mime.startsWith('text/html')) {
      continue
    }

    const html = typeof block.text === 'string' ? block.text : block.blob ? decodeBase64Utf8(block.blob) : null

    if (html !== null) {
      return { ok: true, html, server: read.server }
    }
  }

  return { ok: false, reason: 'not_html' }
}

/** The gateway route (connection + profile) that owns a card's session. */
export interface McpAppOwner {
  connectionId: string | null
  profile: string
}

/** Resolved once, when the card first renders; null when no exact owner is known. */
export type McpAppOwnerPin = Promise<McpAppOwner | null>

/**
 * Pin the connection/profile that owns the session. Never consults the active
 * gateway or profile: an unknown owner resolves to null and the card refuses.
 */
export function pinMcpAppOwner(sessionId: string | null | undefined): McpAppOwnerPin {
  if (!sessionId) {
    return Promise.resolve(null)
  }

  return resolveSessionOwner(sessionId).then(
    scope => {
      if (isSessionOwnerRoute(scope)) {
        return { connectionId: scope.connectionId, profile: scope.profile }
      }

      return typeof scope === 'string' && scope.trim() ? { connectionId: null, profile: scope.trim() } : null
    },
    () => null
  )
}

const NO_OWNER = 'This MCP app is not attached to a session backend; the request was refused.'

async function requestOnOwner<T>(
  owner: McpAppOwnerPin | null | undefined,
  method: string,
  params: Record<string, unknown>
): Promise<T> {
  const route = owner ? await owner : null

  if (!route) {
    throw new Error(NO_OWNER)
  }

  return requestConnectedGatewayForOwner<T>(route.connectionId, route.profile, method, params)
}

/**
 * Read an app resource via the M6a RPC, through the gateway that owns the
 * card's session (never the active one). The read names its server; the backend never guesses.
 */
export async function readMcpAppResource(
  server: string,
  uri: string,
  owner?: McpAppOwnerPin | null
): Promise<McpResourceReadResult> {
  return requestOnOwner<McpResourceReadResult>(owner, 'mcp.resources.read', { server, uri })
}

/**
 * Execute an approved app tool call through the gateway that owns the card's
 * session. The backend runs the registered MCP handler, so the M2 trust gate
 * applies on top of the host approval the bridge already required.
 */
export async function callMcpAppTool(
  request: McpAppToolRequest,
  sessionId?: string | null,
  owner?: McpAppOwnerPin | null
): Promise<McpAppToolResult> {
  // The session is the one that rendered the card. Never fall back to the
  // focused chat: approval gate, cwd and profile belong to the card's session.
  if (!sessionId) {
    throw new Error('This MCP app is not attached to a session; the tool call was refused.')
  }

  try {
    return await requestOnOwner<McpAppToolResult>(owner, 'mcp.tools.call', {
      session_id: sessionId,
      server: request.server,
      name: request.name,
      arguments: request.arguments
    })
  } catch (error) {
    if (isMissingRpcMethod(error)) {
      return {
        isError: true,
        content: [{ type: 'text', text: 'Tool calls from MCP apps are not available on this Hermes backend yet.' }]
      }
    }

    throw error
  }
}
