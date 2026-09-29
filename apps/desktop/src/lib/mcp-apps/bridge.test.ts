// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'

import {
  clampMcpAppHeight,
  createMcpAppBridge,
  MCP_APP_MAX_HEIGHT,
  type McpAppBridgeOptions,
  parseMcpAppMessage
} from './bridge'
import { findMcpAppUri, mcpServerFromToolName, pickMcpAppHtml } from './resolve'
import { buildMcpAppSrcdoc, MCP_APP_CSP, MCP_APP_MAX_HTML_BYTES, MCP_APP_SANDBOX, sanitizeMcpAppTheme } from './sandbox'

const flush = () => new Promise(resolve => setTimeout(resolve, 0))

function fakeFrame() {
  const posted: unknown[] = []
  const frame = { postMessage: vi.fn((message: unknown) => posted.push(message)) } as unknown as Window

  return { frame, posted }
}

function bridgeWith(overrides: Partial<McpAppBridgeOptions> = {}) {
  const { frame, posted } = fakeFrame()
  const approve = vi.fn(async () => true)
  const callTool = vi.fn(async () => ({ structuredContent: { value: 'ok' } }))
  const onResize = vi.fn()

  const bridge = createMcpAppBridge({
    frameWindow: () => frame,
    server: 'fixture-server',
    appUri: 'ui://fixture/app',
    approve,
    callTool,
    onResize,
    ...overrides
  })

  const send = (data: unknown, source: unknown = frame, origin = 'null') =>
    bridge.handleMessage({ data, source, origin } as unknown as MessageEvent)

  return { approve, bridge, callTool, frame, onResize, posted, send }
}

describe('MCP app sandbox document', () => {
  it('uses exactly allow-scripts and never allow-same-origin', () => {
    expect(MCP_APP_SANDBOX).toBe('allow-scripts')
    expect(MCP_APP_SANDBOX.split(/\s+/)).not.toContain('allow-same-origin')
  })

  it('puts the CSP meta first, ahead of any server markup', () => {
    const built = buildMcpAppSrcdoc('<!doctype html><html><head><script>1</script></head><body>x</body></html>')

    expect(built.ok).toBe(true)

    if (!built.ok) {
      return
    }

    expect(
      built.srcdoc.startsWith(`<!doctype html><meta http-equiv="Content-Security-Policy" content="${MCP_APP_CSP}">`)
    ).toBe(true)

    const doc = new DOMParser().parseFromString(built.srcdoc, 'text/html')
    const first = doc.head.firstElementChild

    expect(first?.tagName).toBe('META')
    expect(first?.getAttribute('http-equiv')).toBe('Content-Security-Policy')
    expect(first?.getAttribute('content')).toBe(MCP_APP_CSP)
    // The server's own script lands after the policy.
    expect(doc.querySelectorAll('script').length).toBe(2)
    expect(first?.compareDocumentPosition(doc.querySelectorAll('script')[1]!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
  })

  it('denies all network in the CSP', () => {
    expect(MCP_APP_CSP).toContain("default-src 'none'")
    expect(MCP_APP_CSP).toContain("connect-src 'none'")
    expect(MCP_APP_CSP).toContain("frame-src 'none'")
    expect(MCP_APP_CSP).toContain('img-src data:')
  })

  it('refuses oversize HTML', () => {
    const html = 'a'.repeat(MCP_APP_MAX_HTML_BYTES + 1)

    expect(buildMcpAppSrcdoc(html)).toEqual({ ok: false, reason: 'too_large' })
    expect(buildMcpAppSrcdoc('   ')).toEqual({ ok: false, reason: 'empty' })
  })

  it('drops theme values that could break out of the style block', () => {
    expect(
      sanitizeMcpAppTheme({
        background: 'oklch(0.98 0 0)',
        foreground: 'red;}</style><script>alert(1)</script>',
        primary: '#123456'
      })
    ).toEqual({ background: 'oklch(0.98 0 0)', primary: '#123456' })
  })
})

describe('parseMcpAppMessage', () => {
  it('accepts the two narrow shapes', () => {
    expect(parseMcpAppMessage({ type: 'resize', height: 200 })).toEqual({ type: 'resize', height: 200 })
    expect(parseMcpAppMessage({ type: 'tools/call', id: 1, name: 'echo', arguments: { a: 1 } })).toEqual({
      type: 'tools/call',
      id: 1,
      name: 'echo',
      arguments: { a: 1 }
    })
    expect(parseMcpAppMessage({ type: 'tools/call', name: 'echo' })).toEqual({
      type: 'tools/call',
      name: 'echo',
      arguments: {}
    })
  })

  it.each([
    null,
    'tools/call',
    [],
    { type: 'eval', code: 'x' },
    { type: 'resize', height: '100' },
    { type: 'resize', height: Number.NaN },
    { type: 'resize', height: -1 },
    { type: 'resize', height: 10, extra: true },
    { type: 'tools/call', name: '' },
    { type: 'tools/call', name: 'bad name!' },
    { type: 'tools/call', name: 'x'.repeat(129) },
    { type: 'tools/call', name: 'echo', arguments: [1] },
    { type: 'tools/call', name: 'echo', arguments: 'a=1' },
    { type: 'tools/call', name: 'echo', id: { nested: true } },
    { type: 'tools/call', name: 'echo', server: 'other-server' },
    { type: 'tools/call', name: 'echo', arguments: { big: 'x'.repeat(70 * 1024) } }
  ])('rejects malformed message %#', data => {
    expect(parseMcpAppMessage(data)).toBeNull()
  })
})

describe('createMcpAppBridge', () => {
  it('ignores messages from any window other than the frame', async () => {
    const { callTool, approve, onResize, send } = bridgeWith()

    send({ type: 'resize', height: 300 }, window)
    send({ type: 'tools/call', name: 'echo' }, window)
    send({ type: 'tools/call', name: 'echo' }, null)
    await flush()

    expect(onResize).not.toHaveBeenCalled()
    expect(approve).not.toHaveBeenCalled()
    expect(callTool).not.toHaveBeenCalled()
  })

  it('ignores messages from a non-opaque origin even with the right source', async () => {
    const { approve, send } = bridgeWith()

    send({ type: 'tools/call', name: 'echo' }, undefined, 'file://')
    await flush()

    expect(approve).not.toHaveBeenCalled()
  })

  it('ignores malformed messages', async () => {
    const { approve, onResize, send } = bridgeWith()

    send({ type: 'tools/call', name: 'echo', arguments: 'nope' })
    send({ type: 'resize' })
    send('{"type":"tools/call","name":"echo"}')
    await flush()

    expect(approve).not.toHaveBeenCalled()
    expect(onResize).not.toHaveBeenCalled()
  })

  it('clamps and rate-limits resize', () => {
    const { onResize, send } = bridgeWith({ limits: { resizes: 2 } })

    send({ type: 'resize', height: 99_999 })
    send({ type: 'resize', height: 1 })
    send({ type: 'resize', height: 300 })

    expect(onResize.mock.calls).toEqual([[MCP_APP_MAX_HEIGHT], [clampMcpAppHeight(1)]])
  })

  it('routes tools/call through approval and does not execute when denied', async () => {
    const approve = vi.fn(async () => false)
    const { callTool, posted, send } = bridgeWith({ approve })

    send({ type: 'tools/call', id: 'a', name: 'destructive_action', arguments: {} })
    await flush()

    expect(approve).toHaveBeenCalledWith({
      server: 'fixture-server',
      appUri: 'ui://fixture/app',
      name: 'destructive_action',
      arguments: {}
    })
    expect(callTool).not.toHaveBeenCalled()
    expect(posted).toEqual([
      {
        type: 'tools/call/result',
        id: 'a',
        ok: false,
        error: { code: 'denied', message: 'The user did not approve this tool call. It was not run.' }
      }
    ])
  })

  it('fails closed when the approval function throws', async () => {
    const approve = vi.fn(async () => {
      throw new Error('prompt crashed')
    })

    const { callTool, posted, send } = bridgeWith({ approve })

    send({ type: 'tools/call', name: 'echo' })
    await flush()

    expect(callTool).not.toHaveBeenCalled()
    expect(posted[0]).toMatchObject({ ok: false, error: { code: 'denied' } })
  })

  it('executes an approved call on the app server and replies with only the structured result', async () => {
    const callTool = vi.fn(async () => ({
      structuredContent: { value: 'ok' },
      content: [{ type: 'text', text: 'ok' }],
      hostSecret: 'must-not-leak'
    }))

    const { approve, frame, posted, send } = bridgeWith({ callTool })

    send({ type: 'tools/call', id: 7, name: 'structured_valid', arguments: { q: 1 } })
    await flush()

    expect(approve).toHaveBeenCalledTimes(1)
    expect(callTool).toHaveBeenCalledWith({
      server: 'fixture-server',
      appUri: 'ui://fixture/app',
      name: 'structured_valid',
      arguments: { q: 1 }
    })
    expect(frame.postMessage).toHaveBeenCalledWith(expect.anything(), '*')
    expect(posted).toEqual([
      {
        type: 'tools/call/result',
        id: 7,
        ok: true,
        result: { structuredContent: { value: 'ok' }, content: [{ type: 'text', text: 'ok' }] }
      }
    ])
  })

  it('allows one call in flight and rate-limits bursts', async () => {
    let release: (value: boolean) => void = () => {}
    const approve = vi.fn(() => new Promise<boolean>(resolve => (release = resolve)))
    const { posted, send } = bridgeWith({ approve, limits: { toolCalls: 1 } })

    send({ type: 'tools/call', id: 1, name: 'echo' })
    send({ type: 'tools/call', id: 2, name: 'echo' })
    await flush()
    expect(posted).toEqual([
      expect.objectContaining({ id: 2, ok: false, error: expect.objectContaining({ code: 'busy' }) })
    ])

    release(false)
    await flush()
    send({ type: 'tools/call', id: 3, name: 'echo' })
    await flush()

    expect(approve).toHaveBeenCalledTimes(1)
    expect(posted.at(-1)).toMatchObject({ id: 3, ok: false, error: { code: 'rate_limited' } })
  })

  it('stops answering after dispose, even for a call already approved', async () => {
    let release: (value: boolean) => void = () => {}
    const approve = vi.fn(() => new Promise<boolean>(resolve => (release = resolve)))
    const { bridge, callTool, posted, send } = bridgeWith({ approve })

    send({ type: 'tools/call', name: 'echo' })
    await flush()
    bridge.dispose()
    release(true)
    await flush()

    expect(callTool).not.toHaveBeenCalled()
    expect(posted).toEqual([])
  })
})

describe('resolve helpers', () => {
  it('finds the ui:// app a tool result references', () => {
    expect(findMcpAppUri({ result: 'x', _meta: { ui: { resourceUri: 'ui://fixture/app' } } })).toBe('ui://fixture/app')
    expect(findMcpAppUri('{"result":"x","_meta":{"ui/resourceUri":"ui://fixture/app"}}')).toBe('ui://fixture/app')
    expect(findMcpAppUri({ content: [{ type: 'resource_link', uri: 'ui://fixture/app' }] })).toBe('ui://fixture/app')
    expect(findMcpAppUri({ _meta: { ui: { resourceUri: 'https://evil.example/app' } } })).toBeNull()
    expect(findMcpAppUri('plain text ui://fixture/app')).toBeNull()
  })

  it('reads the server from an MCP registry name', () => {
    expect(mcpServerFromToolName('mcp__fixture__structured_valid')).toBe('fixture')
    expect(mcpServerFromToolName('terminal')).toBeNull()
  })

  it('picks the HTML block from a resource read (text or base64 blob)', () => {
    const read = {
      server: 'fixture',
      uri: 'ui://fixture/app',
      contents: [{ uri: 'ui://fixture/app', mimeType: 'text/html;profile=mcp-app', text: '<main>hi</main>' }]
    }

    expect(pickMcpAppHtml(read)).toEqual({ ok: true, html: '<main>hi</main>', server: 'fixture' })
    expect(
      pickMcpAppHtml({ ...read, contents: [{ uri: read.uri, mimeType: 'text/html', blob: btoa('<p>b</p>') }] })
    ).toEqual({ ok: true, html: '<p>b</p>', server: 'fixture' })
    expect(
      pickMcpAppHtml({ ...read, contents: [{ uri: read.uri, mimeType: 'application/json', text: '{}' }] })
    ).toEqual({
      ok: false,
      reason: 'not_html'
    })
  })
})
