// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MCP_APP_CSP, MCP_APP_MAX_HTML_BYTES } from '@/lib/mcp-apps/sandbox'

import { McpAppCard, McpAppFrame } from './mcp-app-frame'

vi.mock('@assistant-ui/react', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useAuiState: (select: (state: unknown) => unknown) =>
    select({ message: { id: 'msg-1', status: { type: 'complete' } }, thread: { isRunning: false } })
}))

const { ToolFallback } = await import('./fallback')

// The conformance fixture's `ui://fixture/app`, plus the button an app uses
// to call a tool through the bridge.
const FIXTURE_HTML =
  '<!doctype html><html><body><main>MCP fixture app</main>' +
  "<button onclick=\"parent.postMessage({type:'tools/call',id:1,name:'destructive_action',arguments:{}},'*')\">Run</button>" +
  '</body></html>'

const THEME = { background: '#ffffff', foreground: '#111111' }

const readFixture = vi.fn(async (uri: string) => ({
  server: 'hermes-mcp-2026-fixture',
  uri,
  contents: [{ uri, mimeType: 'text/html;profile=mcp-app', text: FIXTURE_HTML }]
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const frameEl = () => document.querySelector('iframe[data-mcp-app]') as HTMLIFrameElement

// The card mounts its frame after an async resource read, which lands outside
// act(); flush once so the bridge's passive effect has attached its listener.
async function frameReady() {
  await waitFor(() => expect(frameEl()).not.toBeNull())
  await act(async () => {})
}

function post(data: unknown, source: unknown, origin = 'null') {
  act(() => {
    window.dispatchEvent(new MessageEvent('message', { data, origin, source: source as Window }))
  })
}

// jsdom never executes the srcdoc (no opaque-origin browsing context), so the
// app's button click is simulated as the exact message it posts.
const clickAppButton = (frame: HTMLIFrameElement) =>
  post({ type: 'tools/call', id: 1, name: 'destructive_action', arguments: {} }, frame.contentWindow)

describe('McpAppFrame', () => {
  it('renders the app in an opaque-origin sandbox with the CSP meta first', () => {
    render(
      <McpAppFrame
        approve={async () => false}
        appUri="ui://fixture/app"
        callTool={vi.fn()}
        html={FIXTURE_HTML}
        server="hermes-mcp-2026-fixture"
        theme={THEME}
      />
    )

    const frame = frameEl()

    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    expect((frame.getAttribute('sandbox') ?? '').split(' ')).not.toContain('allow-same-origin')
    expect((frame.getAttribute('sandbox') ?? '').split(' ')).not.toContain('allow-top-navigation')
    expect((frame.getAttribute('sandbox') ?? '').split(' ')).not.toContain('allow-popups')
    expect((frame.getAttribute('sandbox') ?? '').split(' ')).not.toContain('allow-forms')
    expect(frame.getAttribute('src')).toBeNull()
    expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer')

    const srcdoc = frame.getAttribute('srcdoc') ?? ''
    const doc = new DOMParser().parseFromString(srcdoc, 'text/html')

    expect(doc.head.firstElementChild?.getAttribute('http-equiv')).toBe('Content-Security-Policy')
    expect(doc.head.firstElementChild?.getAttribute('content')).toBe(MCP_APP_CSP)
    expect(doc.body.textContent).toContain('MCP fixture app')
    expect(srcdoc).toContain('--hermes-background:#ffffff')
  })

  it('refuses oversize HTML with an inline error and no frame', () => {
    render(
      <McpAppFrame
        approve={async () => false}
        appUri="ui://fixture/app"
        callTool={vi.fn()}
        html={`<p>${'x'.repeat(MCP_APP_MAX_HTML_BYTES)}</p>`}
        server="s"
        theme={THEME}
      />
    )

    expect(frameEl()).toBeNull()
    expect(screen.getByRole('status').textContent).toContain('too large')
  })

  it('carries no host data into the app document', () => {
    document.cookie = 'hermes_session=host-secret'
    localStorage.setItem('hermes-token', 'host-secret')

    render(
      <McpAppFrame
        approve={async () => false}
        appUri="ui://fixture/app"
        callTool={vi.fn()}
        html={FIXTURE_HTML}
        server="s"
        theme={THEME}
      />
    )

    expect(frameEl().getAttribute('srcdoc')).not.toContain('host-secret')
    localStorage.removeItem('hermes-token')
  })

  /*
   * Opaque origin: the real guarantee is the browser's. A document in
   * `sandbox="allow-scripts"` (no allow-same-origin) is cross-origin to the
   * host, so `parent.document`, `document.cookie`, `localStorage` and
   * `parent.hermesDesktop` throw SecurityError in Chromium/Electron. jsdom
   * does not model sandboxed browsing contexts or opaque origins — a jsdom
   * iframe window can read its parent — so this test pins the attributes that
   * produce the isolation and asserts the bridge is the only channel, rather
   * than claiming jsdom proves the throw.
   */
  it('cannot reach the host: sandbox tokens pin an opaque origin; jsdom limitation documented', () => {
    render(
      <McpAppFrame
        approve={async () => false}
        appUri="ui://fixture/app"
        callTool={vi.fn()}
        html={FIXTURE_HTML}
        server="s"
        theme={THEME}
      />
    )

    const frame = frameEl()
    const tokens = (frame.getAttribute('sandbox') ?? '').split(' ')

    expect(tokens).toEqual(['allow-scripts'])

    // What jsdom does model: the frame is a separate window whose document is
    // not the host document.
    const win = frame.contentWindow

    expect(win).not.toBeNull()
    expect(win?.document).not.toBe(document)
  })
})

describe('McpAppCard', () => {
  it('renders the fixture app inline with a header naming app and server', async () => {
    render(
      <McpAppCard
        readResource={readFixture}
        theme={THEME}
        toolName="mcp__hermes_mcp_2026_fixture__open_app"
        uri="ui://fixture/app"
      />
    )

    await frameReady()

    expect(readFixture).toHaveBeenCalledWith('ui://fixture/app')
    expect(screen.getByText('fixture/app')).toBeTruthy()
    expect(screen.getByText('· hermes-mcp-2026-fixture')).toBeTruthy()
  })

  it('shows an inline error when the resource is not HTML', async () => {
    const readJson = vi.fn(async (uri: string) => ({
      server: 's',
      uri,
      contents: [{ uri, mimeType: 'application/json', text: '{}' }]
    }))

    render(<McpAppCard readResource={readJson} theme={THEME} toolName="mcp__s__t" uri="ui://s/app" />)

    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('not an HTML app'))
    expect(frameEl()).toBeNull()
  })

  it("the app's button asks for approval; Deny does not run the tool", async () => {
    const callTool = vi.fn(async () => ({ structuredContent: { ok: true } }))

    render(
      <McpAppCard
        callTool={callTool}
        readResource={readFixture}
        theme={THEME}
        toolName="mcp__f__t"
        uri="ui://fixture/app"
      />
    )

    await frameReady()
    const frame = frameEl()
    const reply = vi.spyOn(frame.contentWindow!, 'postMessage')

    clickAppButton(frame)

    const prompt = await screen.findByRole('alertdialog')

    expect(prompt.textContent).toContain('destructive_action')

    fireEvent.click(screen.getByRole('button', { name: 'Deny' }))

    await waitFor(() =>
      expect(reply).toHaveBeenCalledWith(
        expect.objectContaining({ id: 1, ok: false, error: expect.objectContaining({ code: 'denied' }) }),
        '*'
      )
    )
    expect(callTool).not.toHaveBeenCalled()
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('Allow once runs the tool on the app server and replies to the frame', async () => {
    const callTool = vi.fn(async () => ({ structuredContent: { ok: true } }))

    render(
      <McpAppCard
        callTool={callTool}
        readResource={readFixture}
        theme={THEME}
        toolName="mcp__f__t"
        uri="ui://fixture/app"
      />
    )

    await frameReady()
    const frame = frameEl()
    const reply = vi.spyOn(frame.contentWindow!, 'postMessage')

    clickAppButton(frame)
    fireEvent.click(await screen.findByRole('button', { name: 'Allow once' }))

    await waitFor(() =>
      expect(reply).toHaveBeenCalledWith(
        { type: 'tools/call/result', id: 1, ok: true, result: { structuredContent: { ok: true } } },
        '*'
      )
    )
    expect(callTool).toHaveBeenCalledWith({
      server: 'hermes-mcp-2026-fixture',
      appUri: 'ui://fixture/app',
      name: 'destructive_action',
      arguments: {}
    })
  })

  it('ignores tool calls posted by another window and malformed ones', async () => {
    const callTool = vi.fn()

    render(
      <McpAppCard
        callTool={callTool}
        readResource={readFixture}
        theme={THEME}
        toolName="mcp__f__t"
        uri="ui://fixture/app"
      />
    )

    await frameReady()
    const frame = frameEl()

    post({ type: 'tools/call', name: 'destructive_action' }, window)
    post({ type: 'tools/call', name: 'destructive_action', sneaky: 1 }, frame.contentWindow)
    post({ type: 'tools/call', name: 'destructive_action' }, frame.contentWindow, 'https://evil.example')

    await new Promise(resolve => setTimeout(resolve, 0))

    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(callTool).not.toHaveBeenCalled()
  })

  it('resizes the frame from a valid resize message', async () => {
    render(<McpAppCard readResource={readFixture} theme={THEME} toolName="mcp__f__t" uri="ui://fixture/app" />)

    await frameReady()
    const frame = frameEl()

    post({ type: 'resize', height: 222 }, frame.contentWindow)

    expect(frame.style.height).toBe('222px')
  })

  it('closes the app when its document navigates away (second load)', async () => {
    render(<McpAppCard readResource={readFixture} theme={THEME} toolName="mcp__f__t" uri="ui://fixture/app" />)

    await frameReady()
    const frame = frameEl()

    fireEvent.load(frame)
    fireEvent.load(frame)

    await waitFor(() => expect(frameEl()).toBeNull())
    expect(screen.getByRole('status').textContent).toContain('tried to leave')
  })
})

describe('transcript placement', () => {
  const renderTool = (toolName: string, result: unknown) =>
    render(
      <ToolFallback
        {...({ args: {}, result, toolCallId: 'call-1', toolName } as unknown as ComponentProps<typeof ToolFallback>)}
      />
    )

  it('mounts the app card under an MCP tool row whose result references a ui:// app', () => {
    renderTool('mcp__hermes_mcp_2026_fixture__open_app', {
      result: 'opened',
      _meta: { ui: { resourceUri: 'ui://fixture/app' } }
    })

    expect(document.querySelector('[data-slot="mcp-app-card"]')).not.toBeNull()
  })

  it('does not mount an app for non-MCP tools or results without a ui:// reference', () => {
    renderTool('web_extract', { result: 'x', _meta: { ui: { resourceUri: 'ui://fixture/app' } } })
    renderTool('mcp__f__structured_valid', { result: 'x' })

    expect(document.querySelector('[data-slot="mcp-app-card"]')).toBeNull()
  })
})
