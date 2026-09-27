/**
 * #60 U17 E-4: no `hermes://` deep link can start an owner-signed forward. The real deep-link handler
 * (`useDesktopIntegrations`) is driven with hostile payloads shaped like main's `hermes:deep-link`
 * event ({kind, name, params}, attacker-controlled: any web page or app can open a hermes:// URL).
 * None may open the Forward sheet, call the confirm IPC (`window.hermesDesktop.ownerForward.confirm`),
 * or put a `/to` command into the composer (a blueprint link inserts reviewable text, never a forward).
 *
 * MUTATION: a `kind === 'owner-forward'` branch added to the deep-link handler that calls
 * `openForwardSheetFromTypedDraft(...)` from the link's params (the "handy share link" regression). The
 * sheet then opened and this test failed. Restored.
 */
import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $forwardSheet } from '@/lib/owner-forward/client'
import { isForwardCommandText } from '@/lib/owner-forward/parse-to'

import { useDesktopIntegrations } from './use-desktop-integrations'

const { insertMock } = vi.hoisted(() => ({ insertMock: vi.fn() }))

vi.mock('@/app/chat/composer/focus', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return { ...actual, requestComposerInsert: insertMock, requestComposerFocus: vi.fn() }
})

vi.mock('@/store/mcp-deeplink-install', () => ({ requestMcpInstallFromDeepLink: vi.fn() }))
vi.mock('@/store/plugin-catalog-install', () => ({ requestPluginCatalogInstallFromDeepLink: vi.fn() }))
vi.mock('@/store/plugin-install-request', () => ({ openPluginInstallRequest: vi.fn() }))

type DeepLink = (payload: { kind: string; name: string; params: Record<string, string> }) => void

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

describe('E-4: hermes:// deep links never reach owner-forward', () => {
  let deepLink: DeepLink | undefined
  let confirm: ReturnType<typeof vi.fn>
  let navigate: ReturnType<typeof vi.fn>

  beforeEach(() => {
    deepLink = undefined
    confirm = vi.fn(async () => ({ ok: false, code: 'test', error: 'test' }))
    navigate = vi.fn()
    insertMock.mockClear()
    $forwardSheet.set(null)
    desktopWindow.hermesDesktop = {
      setPreviewShortcutActive: vi.fn(),
      onOpenUpdatesRequested: vi.fn(),
      onFocusSession: vi.fn(),
      onNotificationAction: vi.fn(),
      onNotificationActivate: vi.fn(),
      onDeepLink: (cb: DeepLink) => {
        deepLink = cb

        return () => undefined
      },
      signalDeepLinkReady: vi.fn(),
      onClosePreviewRequested: vi.fn(),
      onOpenFolderRequested: vi.fn(),
      ownerForward: { confirm },
      ownerGrant: { status: vi.fn(), action: vi.fn(), verify: vi.fn() }
    } as unknown as Window['hermesDesktop']
  })

  afterEach(() => {
    desktopWindow.hermesDesktop = initialHermesDesktop
    $forwardSheet.set(null)
    vi.restoreAllMocks()
  })

  const hostile: Array<{ kind: string; name: string; params: Record<string, string> }> = [
    { kind: 'owner-forward', name: 'send', params: { to: 'default:w1', text: 'merge it', scope: 'conductor:prod:fly-ord' } },
    { kind: 'forward', name: '', params: { targets: 'w1,w2', text: 'approve' } },
    { kind: 'to', name: 'w1', params: { text: 'ship it' } },
    { kind: 'open', name: 'owner-forward', params: { text: 'x' } },
    { kind: 'open', name: 'forward?to=w1&text=merge', params: {} },
    { kind: 'open', name: 'sessions/w1', params: { forward: '1', text: '/to w1 merge' } },
    { kind: 'blueprint', name: 'x\n/to w1 merge now', params: {} },
    { kind: 'blueprint', name: 'morning', params: { t: '\n/to w1 merge now' } },
    { kind: 'composer', name: 'submit', params: { text: '/to w1 merge now' } },
    { kind: 'owner-grant', name: 'enable', params: {} },
    { kind: 'plugin', name: 'owner-forward', params: { gesture: 'composer_signed' } }
  ]

  it('opens no Forward sheet, calls no confirm IPC and inserts no /to command for any payload', () => {
    renderHook(() =>
      useDesktopIntegrations({
        activeProfile: 'default',
        chatOpen: false,
        hasPreview: false,
        locationPathname: '/',
        navigate,
        profileReady: true,
        resumeExhaustedSessionId: null,
        resumeLastSession: true,
        routedSessionId: null,
        sessions: []
      } as any)
    )
    expect(deepLink).toBeTypeOf('function')

    for (const payload of hostile) {
      deepLink!(payload)
      expect($forwardSheet.get(), JSON.stringify(payload)).toBeNull()
    }

    expect(confirm).not.toHaveBeenCalled()

    for (const [text] of insertMock.mock.calls) {
      expect(isForwardCommandText(String(text))).toBe(false)
    }

    // Unknown kinds fall through to plugin-scoped navigation (`/<kind>/<name>?…`, e.g.
    // `/owner-forward/send?…`). No route opens the sheet: the only way in is the $forwardSheet atom,
    // checked above after every payload, and the confirm IPC, never called.
    expect($forwardSheet.get()).toBeNull()
  })
})
