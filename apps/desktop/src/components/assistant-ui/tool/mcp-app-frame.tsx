import { type FC, useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import {
  createMcpAppBridge,
  type McpAppApprove,
  type McpAppCallTool,
  type McpAppToolRequest
} from '@/lib/mcp-apps/bridge'
import {
  callMcpAppTool,
  mcpAppLabel,
  type McpResourceReadResult,
  mcpServerFromToolName,
  pickMcpAppHtml,
  readMcpAppResource
} from '@/lib/mcp-apps/resolve'
import {
  buildMcpAppSrcdoc,
  MCP_APP_MAX_HTML_BYTES,
  MCP_APP_PERMISSIONS_POLICY,
  MCP_APP_SANDBOX,
  type McpAppTheme,
  readHostMcpAppTheme
} from '@/lib/mcp-apps/sandbox'

const INITIAL_HEIGHT = 120

export interface McpAppFrameProps {
  html: string
  server: string
  appUri: string
  approve: McpAppApprove
  callTool: McpAppCallTool
  /** Theme tokens for the app; defaults to the host's current tokens. */
  theme?: McpAppTheme
}

/**
 * The sandboxed app surface. Opaque origin (`sandbox="allow-scripts"` only),
 * CSP-first srcdoc, and a bridge that listens only to this frame's window.
 * A second `load` means the document navigated itself away from the srcdoc:
 * the frame is torn down and the bridge stops answering.
 */
export const McpAppFrame: FC<McpAppFrameProps> = ({ appUri, approve, callTool, html, server, theme }) => {
  const { t } = useI18n()
  const frameRef = useRef<HTMLIFrameElement>(null)
  // Loads counted per srcdoc: a second load of the same document means it
  // navigated itself away.
  const loads = useRef<{ count: number; srcdoc: string }>({ count: 0, srcdoc: '' })
  const [height, setHeight] = useState(INITIAL_HEIGHT)
  const [navigatedAway, setNavigatedAway] = useState(false)

  const resolvedTheme = useMemo(() => theme ?? readHostMcpAppTheme(), [theme])
  const built = useMemo(() => buildMcpAppSrcdoc(html, resolvedTheme), [html, resolvedTheme])

  useEffect(() => {
    if (!built.ok || navigatedAway) {
      return
    }

    const bridge = createMcpAppBridge({
      frameWindow: () => frameRef.current?.contentWindow,
      server,
      appUri,
      approve,
      callTool,
      onResize: setHeight
    })

    window.addEventListener('message', bridge.handleMessage)

    return () => {
      window.removeEventListener('message', bridge.handleMessage)
      bridge.dispose()
    }
  }, [appUri, approve, built, callTool, navigatedAway, server])

  const srcdoc = built.ok ? built.srcdoc : ''

  const onLoad = useCallback(() => {
    if (loads.current.srcdoc !== srcdoc) {
      loads.current = { count: 0, srcdoc }
    }

    loads.current.count += 1

    if (loads.current.count > 1) {
      setNavigatedAway(true)
    }
  }, [srcdoc])

  if (!built.ok) {
    return (
      <McpAppNotice>
        {built.reason === 'too_large'
          ? t.assistant.mcpApp.tooLarge(MCP_APP_MAX_HTML_BYTES / 1024)
          : t.assistant.mcpApp.notHtml}
      </McpAppNotice>
    )
  }

  if (navigatedAway) {
    return <McpAppNotice>{t.assistant.mcpApp.navigatedAway}</McpAppNotice>
  }

  return (
    <iframe
      allow={MCP_APP_PERMISSIONS_POLICY}
      className="block w-full border-0 bg-transparent"
      data-mcp-app={appUri}
      // Electron main guards sub-frame navigation only for frames with this name.
      name="hermes-mcp-app"
      onLoad={onLoad}
      ref={frameRef}
      referrerPolicy="no-referrer"
      sandbox={MCP_APP_SANDBOX}
      srcDoc={built.srcdoc}
      style={{ height }}
      title={t.assistant.mcpApp.frameTitle(mcpAppLabel(appUri), server)}
    />
  )
}

const McpAppNotice: FC<{ children: string }> = ({ children }) => (
  <div className="px-2.5 py-2 text-xs text-(--ui-text-secondary)" role="status">
    {children}
  </div>
)

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; reason: 'load_failed' | 'not_html' }
  | { status: 'ready'; html: string; server: string }

interface PendingApproval {
  request: McpAppToolRequest
  resolve: (approved: boolean) => void
}

export interface McpAppCardProps {
  uri: string
  toolName: string
  /** Injection seams for tests; default to the gateway RPCs. */
  readResource?: (server: string, uri: string) => Promise<McpResourceReadResult>
  /** Receives the card's pinned session id as its second argument. */
  callTool?: (request: McpAppToolRequest, sessionId: string | null) => ReturnType<McpAppCallTool>
  /** Session whose transcript rendered this card. Tool calls are pinned to it, never to the focused chat. */
  sessionId?: string | null
  theme?: McpAppTheme
}

/**
 * Transcript card for a tool result that references a `ui://` app: a compact
 * header (badge, app, server), the sandboxed frame, and an inline one-shot
 * approval row for every tool call the app asks for. There is no auto-approve
 * path: the call waits until the user picks Allow once or Deny, and unmounting
 * the card denies it.
 */
export const McpAppCard: FC<McpAppCardProps> = ({
  callTool = callMcpAppTool,
  readResource = readMcpAppResource,
  sessionId = null,
  theme,
  toolName,
  uri
}) => {
  const { t } = useI18n()
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [pending, setPending] = useState<PendingApproval | null>(null)
  const pendingRef = useRef<PendingApproval | null>(null)

  // Pinned to the session that rendered the card (first known id wins), so a
  // later focus change can never redirect an approved call to another chat.
  const pinnedSession = useRef<string | null>(sessionId)

  if (!pinnedSession.current && sessionId) {
    pinnedSession.current = sessionId
  }

  const pinnedCallTool = useCallback<McpAppCallTool>(request => callTool(request, pinnedSession.current), [callTool])

  // The resource read names its server: the one whose tool result pointed at the app.
  const toolServer = mcpServerFromToolName(toolName)

  useEffect(() => {
    let cancelled = false

    setState({ status: 'loading' })

    if (!toolServer) {
      setState({ status: 'error', reason: 'load_failed' })

      return
    }

    readResource(toolServer, uri).then(
      read => {
        if (cancelled) {
          return
        }

        const picked = pickMcpAppHtml(read)

        setState(
          picked.ok
            ? { status: 'ready', html: picked.html, server: picked.server }
            : { status: 'error', reason: 'not_html' }
        )
      },
      () => {
        if (!cancelled) {
          setState({ status: 'error', reason: 'load_failed' })
        }
      }
    )

    return () => {
      cancelled = true
    }
  }, [readResource, toolServer, uri])

  // Unmount (or a new app) denies whatever was waiting.
  useEffect(
    () => () => {
      pendingRef.current?.resolve(false)
      pendingRef.current = null
    },
    [uri]
  )

  const approve = useCallback<McpAppApprove>(
    request =>
      new Promise<boolean>(resolve => {
        pendingRef.current?.resolve(false)

        // No known session: refuse outright instead of prompting for a call that cannot run.
        if (!pinnedSession.current) {
          resolve(false)

          return
        }

        const entry: PendingApproval = {
          request,
          resolve: approved => {
            if (pendingRef.current === entry) {
              pendingRef.current = null
              setPending(null)
            }

            resolve(approved)
          }
        }

        pendingRef.current = entry
        setPending(entry)
      }),
    []
  )

  const server = state.status === 'ready' ? state.server : (mcpServerFromToolName(toolName) ?? '')
  const label = mcpAppLabel(uri)

  return (
    <section
      aria-label={t.assistant.mcpApp.frameTitle(label, server)}
      className="my-1 overflow-hidden rounded-md shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--ui-stroke-secondary)_50%,transparent)]"
      data-slot="mcp-app-card"
    >
      <header className="flex min-w-0 items-center gap-1.5 px-2.5 py-1 text-xs text-(--ui-text-secondary)">
        <span className="rounded-sm bg-(--ui-bg-quaternary) px-1 text-[0.6875rem] leading-4 text-(--ui-text-primary)">
          {t.assistant.mcpApp.badge}
        </span>
        <span className="truncate text-(--ui-text-primary)">{label}</span>
        {server ? <span className="truncate">· {server}</span> : null}
      </header>

      {pending ? (
        <div
          className="flex flex-wrap items-center gap-2 px-2.5 py-1.5 text-xs text-(--ui-text-primary)"
          data-slot="mcp-app-approval"
          role="alertdialog"
        >
          <span className="min-w-0 flex-1">{t.assistant.mcpApp.wantsToRun(pending.request.name)}</span>
          <Button onClick={() => pending.resolve(false)} size="xs" variant="ghost">
            {t.assistant.mcpApp.deny}
          </Button>
          <Button onClick={() => pending.resolve(true)} size="xs" variant="secondary">
            {t.assistant.mcpApp.allowOnce}
          </Button>
        </div>
      ) : null}

      {state.status === 'loading' ? <McpAppNotice>{t.assistant.mcpApp.loading}</McpAppNotice> : null}
      {state.status === 'error' ? (
        <McpAppNotice>
          {state.reason === 'not_html' ? t.assistant.mcpApp.notHtml : t.assistant.mcpApp.loadFailed}
        </McpAppNotice>
      ) : null}
      {state.status === 'ready' ? (
        <McpAppFrame
          approve={approve}
          appUri={uri}
          callTool={pinnedCallTool}
          html={state.html}
          server={state.server}
          theme={theme}
        />
      ) : null}
    </section>
  )
}
