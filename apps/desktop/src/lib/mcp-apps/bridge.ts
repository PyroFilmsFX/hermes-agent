/**
 * MCP Apps (unit M9): the host side of the app <-> host postMessage bridge.
 *
 * The app document is untrusted. The bridge therefore:
 * - accepts a message only when `event.source` is that frame's own
 *   `contentWindow` and `event.origin` is the opaque origin (`"null"`);
 * - accepts exactly two shapes and rejects anything else, including extra keys:
 *     { type: 'tools/call', id?, name, arguments? }
 *     { type: 'resize', height }
 * - rate-limits both, and allows one tool call in flight at a time;
 * - asks `approve` for EVERY tool call (a click inside the app is not the
 *   user's consent to the host) and never runs the call when approval is
 *   denied, throws, or the bridge is disposed while waiting;
 * - only ever calls tools on the server that served the app;
 * - replies with postMessage(targetOrigin '*') — the frame's origin is opaque,
 *   so '*' is the only deliverable target — carrying nothing but the tool's
 *   own structured result, JSON-cloned and size-capped. No host data.
 */

export const MCP_APP_TOOL_NAME = /^[A-Za-z0-9_.\-/]{1,128}$/
export const MCP_APP_MAX_ARGUMENT_BYTES = 64 * 1024
export const MCP_APP_MAX_RESULT_BYTES = 256 * 1024
export const MCP_APP_MIN_HEIGHT = 48
export const MCP_APP_MAX_HEIGHT = 640

export interface McpAppToolCallMessage {
  type: 'tools/call'
  id?: number | string
  name: string
  arguments: Record<string, unknown>
}

export interface McpAppResizeMessage {
  type: 'resize'
  height: number
}

export type McpAppMessage = McpAppResizeMessage | McpAppToolCallMessage

export interface McpAppToolRequest {
  /** The server that served the app; calls never leave it. */
  server: string
  /** The `ui://` resource the request came from. */
  appUri: string
  /** Tool name on `server`, as the app asked for it. */
  name: string
  arguments: Record<string, unknown>
}

export interface McpAppToolResult {
  isError?: boolean
  content?: unknown
  structuredContent?: unknown
}

/** Resolves true only on an explicit user approval for this one call. */
export type McpAppApprove = (request: McpAppToolRequest) => Promise<boolean>
export type McpAppCallTool = (request: McpAppToolRequest) => Promise<McpAppToolResult>

export type McpAppReply =
  | { type: 'tools/call/result'; id?: number | string; ok: true; result: McpAppToolResult }
  | { type: 'tools/call/result'; id?: number | string; ok: false; error: { code: McpAppErrorCode; message: string } }

export type McpAppErrorCode = 'busy' | 'denied' | 'failed' | 'rate_limited' | 'result_too_large'

export interface McpAppRateLimits {
  /** Max tool calls per `toolCallWindowMs`. */
  toolCalls: number
  toolCallWindowMs: number
  /** Max resize messages per `resizeWindowMs`. */
  resizes: number
  resizeWindowMs: number
}

export const DEFAULT_MCP_APP_RATE_LIMITS: McpAppRateLimits = {
  toolCalls: 5,
  toolCallWindowMs: 60_000,
  resizes: 30,
  resizeWindowMs: 1_000
}

export interface McpAppBridgeOptions {
  /** The frame's current contentWindow; the only accepted `event.source`. */
  frameWindow: () => Window | null | undefined
  server: string
  appUri: string
  approve: McpAppApprove
  callTool: McpAppCallTool
  onResize?: (height: number) => void
  limits?: Partial<McpAppRateLimits>
  now?: () => number
}

export interface McpAppBridge {
  handleMessage: (event: MessageEvent) => void
  dispose: () => void
}

const isPlainObject = (value: unknown): value is Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }

  const proto = Object.getPrototypeOf(value)

  return proto === Object.prototype || proto === null
}

const onlyKeys = (value: Record<string, unknown>, allowed: readonly string[]) =>
  Object.keys(value).every(key => allowed.includes(key))

/** JSON-clone a value under a byte cap; undefined when it cannot be cloned. */
function jsonClone(value: unknown, maxBytes: number): { ok: true; value: unknown } | { ok: false } {
  try {
    const text = JSON.stringify(value)

    if (text === undefined) {
      return { ok: true, value: undefined }
    }

    if (new TextEncoder().encode(text).byteLength > maxBytes) {
      return { ok: false }
    }

    return { ok: true, value: JSON.parse(text) }
  } catch {
    return { ok: false }
  }
}

/** Validate one message from an app. Returns null for anything off-schema. */
export function parseMcpAppMessage(data: unknown): McpAppMessage | null {
  if (!isPlainObject(data)) {
    return null
  }

  if (data.type === 'resize') {
    if (!onlyKeys(data, ['type', 'height'])) {
      return null
    }

    const height = data.height

    if (typeof height !== 'number' || !Number.isFinite(height) || height < 0 || height > 100_000) {
      return null
    }

    return { type: 'resize', height }
  }

  if (data.type === 'tools/call') {
    if (!onlyKeys(data, ['type', 'id', 'name', 'arguments'])) {
      return null
    }

    const { id, name } = data

    if (typeof name !== 'string' || !MCP_APP_TOOL_NAME.test(name)) {
      return null
    }

    if (
      id !== undefined &&
      !(typeof id === 'string' && id.length <= 64) &&
      !(typeof id === 'number' && Number.isFinite(id))
    ) {
      return null
    }

    const rawArgs = data.arguments === undefined ? {} : data.arguments

    if (!isPlainObject(rawArgs)) {
      return null
    }

    const cloned = jsonClone(rawArgs, MCP_APP_MAX_ARGUMENT_BYTES)

    if (!cloned.ok || !isPlainObject(cloned.value)) {
      return null
    }

    return { type: 'tools/call', ...(id !== undefined && { id }), name, arguments: cloned.value }
  }

  return null
}

export const clampMcpAppHeight = (height: number) =>
  Math.min(MCP_APP_MAX_HEIGHT, Math.max(MCP_APP_MIN_HEIGHT, Math.round(height)))

class SlidingWindow {
  private stamps: number[] = []

  constructor(
    private readonly max: number,
    private readonly windowMs: number
  ) {}

  take(now: number): boolean {
    this.stamps = this.stamps.filter(t => now - t < this.windowMs)

    if (this.stamps.length >= this.max) {
      return false
    }

    this.stamps.push(now)

    return true
  }
}

function sanitizeResult(result: unknown): McpAppToolResult | null {
  const record = isPlainObject(result) ? result : {}

  const picked: McpAppToolResult = {
    ...(record.isError === true && { isError: true }),
    ...(record.content !== undefined && { content: record.content }),
    ...(record.structuredContent !== undefined && { structuredContent: record.structuredContent })
  }

  const cloned = jsonClone(picked, MCP_APP_MAX_RESULT_BYTES)

  return cloned.ok ? (cloned.value as McpAppToolResult) : null
}

export function createMcpAppBridge(options: McpAppBridgeOptions): McpAppBridge {
  const limits = { ...DEFAULT_MCP_APP_RATE_LIMITS, ...options.limits }
  const now = options.now ?? (() => Date.now())
  const toolWindow = new SlidingWindow(limits.toolCalls, limits.toolCallWindowMs)
  const resizeWindow = new SlidingWindow(limits.resizes, limits.resizeWindowMs)
  let disposed = false
  let inFlight = false

  const reply = (target: Window, message: McpAppReply) => {
    // Only reply to the same, still-mounted frame the request came from.
    if (disposed || options.frameWindow() !== target) {
      return
    }

    target.postMessage(message, '*')
  }

  const fail = (target: Window, id: McpAppToolCallMessage['id'], code: McpAppErrorCode, message: string) =>
    reply(target, { type: 'tools/call/result', ...(id !== undefined && { id }), ok: false, error: { code, message } })

  const runToolCall = async (target: Window, message: McpAppToolCallMessage) => {
    const { id } = message

    if (inFlight) {
      fail(target, id, 'busy', 'Another tool call from this app is still pending.')

      return
    }

    if (!toolWindow.take(now())) {
      fail(target, id, 'rate_limited', 'Too many tool calls from this app. Try again later.')

      return
    }

    const request: McpAppToolRequest = {
      server: options.server,
      appUri: options.appUri,
      name: message.name,
      arguments: message.arguments
    }

    inFlight = true

    try {
      let approved = false

      try {
        approved = (await options.approve(request)) === true
      } catch {
        approved = false // fail closed
      }

      if (!approved || disposed) {
        fail(target, id, 'denied', 'The user did not approve this tool call. It was not run.')

        return
      }

      let result: McpAppToolResult

      try {
        result = await options.callTool(request)
      } catch {
        fail(target, id, 'failed', 'The tool call failed.')

        return
      }

      const safe = sanitizeResult(result)

      if (!safe) {
        fail(target, id, 'result_too_large', 'The tool result is too large to return to the app.')

        return
      }

      reply(target, { type: 'tools/call/result', ...(id !== undefined && { id }), ok: true, result: safe })
    } finally {
      inFlight = false
    }
  }

  const handleMessage = (event: MessageEvent) => {
    if (disposed) {
      return
    }

    const frame = options.frameWindow()

    // Identity, not origin, is the gate: only this frame's own window. An
    // opaque-origin sandboxed document always reports origin "null".
    if (!frame || event.source !== frame || event.origin !== 'null') {
      return
    }

    const message = parseMcpAppMessage(event.data)

    if (!message) {
      return
    }

    if (message.type === 'resize') {
      if (resizeWindow.take(now())) {
        options.onResize?.(clampMcpAppHeight(message.height))
      }

      return
    }

    void runToolCall(frame, message)
  }

  return {
    handleMessage,
    dispose: () => {
      disposed = true
    }
  }
}
