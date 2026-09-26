import fs from 'node:fs'
import path from 'node:path'

import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $composerPopout, $composerPopoutGesturesEnabled } from '@/store/composer-popout'
import { $threadScrolledUpBySession } from '@/store/thread-scroll'

import { ComposerScopeProvider, ComposerSurfaceProvider, MAIN_COMPOSER_SCOPE } from './scope'
import type { ChatBarState } from './types'

import { ChatBar, ChatBarFallback } from './index'

const stylesCss = fs.readFileSync(path.resolve(__dirname, '../../../styles.css'), 'utf8')

function Runtime({ children }: { children: ReactNode }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [],
    isRunning: false,
    onNew: async () => {}
  })

  return <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
}

const defaultChatBarState: ChatBarState = {
  model: { canSwitch: false, model: '', provider: '' },
  tools: { enabled: false, label: '' },
  voice: { active: false, enabled: false }
}

function renderChatBar({
  busy = false,
  sessionId = 'test-session'
}: {
  busy?: boolean
  sessionId?: string
} = {}) {
  return render(
    <MemoryRouter>
      <I18nProvider configClient={null} initialLocale="en">
        <Runtime>
          <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, target: 'main' }}>
            <ComposerSurfaceProvider value={sessionId}>
              <ChatBar
                busy={busy}
                cwd="/test"
                disabled={false}
                focusKey={sessionId}
                gateway={null}
                maxRecordingSeconds={120}
                onAddUrl={vi.fn()}
                onAttachDroppedItems={vi.fn()}
                onAttachImageBlob={vi.fn()}
                onAttachPastedText={vi.fn()}
                onCancel={vi.fn()}
                onPasteClipboardImage={vi.fn()}
                onPickFiles={vi.fn()}
                onPickFolders={vi.fn()}
                onPickImages={vi.fn()}
                onRemoveAttachment={vi.fn()}
                onSteer={vi.fn()}
                onSteerHidden={vi.fn()}
                onSubmit={vi.fn()}
                onTranscribeAudio={vi.fn()}
                queueSessionKey={sessionId}
                sessionId={sessionId}
                state={defaultChatBarState}
              />
            </ComposerSurfaceProvider>
          </ComposerScopeProvider>
        </Runtime>
      </I18nProvider>
    </MemoryRouter>
  )
}

describe('Composer container backdrop blur styling across states (U1.5)', () => {
  beforeAll(() => {
    const styleEl = document.createElement('style')
    styleEl.textContent = stylesCss
    document.head.appendChild(styleEl)
  })

  afterEach(() => {
    cleanup()
    $threadScrolledUpBySession.set({})
    $composerPopoutGesturesEnabled.set(true)
    $composerPopout.set({ poppedOut: false, position: { bottom: 24, right: 24 } })
  })

  it('pins backdrop-filter blur style on composer-root and underlay in CSS stylesheet', () => {
    // Verify CSS declarations for composer container and its states
    expect(stylesCss).toMatch(/\[data-slot='composer-root'\]\s*\{[^}]*backdrop-filter:\s*blur\(0\.75rem\)/)
    expect(stylesCss).toMatch(/\[data-slot='composer-root'\]\s*\{[^}]*-webkit-backdrop-filter:\s*blur\(0\.75rem\)/)
    expect(stylesCss).toMatch(/\[data-slot='composer-root'\]\[data-popped-out\]\s*\{[^}]*backdrop-filter:\s*blur\(0\.75rem\)/)
    expect(stylesCss).toMatch(/\[data-slot='composer-root'\]\[data-thread-scrolled-up\]\s*\{[^}]*backdrop-filter:\s*blur\(0\.75rem\)/)
    expect(stylesCss).toMatch(/\[data-slot='composer-root'\]\s*>\s*\.pointer-events-none\s*\{[^}]*backdrop-filter:\s*blur\(0\.75rem\)/)
  })

  it('renders composer-root and underlay matching the blur styles in default docked state', () => {
    const { container } = renderChatBar()
    const root = container.querySelector<HTMLElement>('[data-slot="composer-root"]')!
    expect(root).toBeTruthy()

    const underlay = root.querySelector<HTMLElement>(':scope > .pointer-events-none')!
    expect(underlay).toBeTruthy()

    // Matching selectors in document that have backdrop-filter applied
    expect(root.matches("[data-slot='composer-root']")).toBe(true)
    expect(underlay.matches("[data-slot='composer-root'] > .pointer-events-none")).toBe(true)
  })

  it('preserves backdrop blur styling when a reply scrolls under composer (data-thread-scrolled-up)', () => {
    $threadScrolledUpBySession.set({ 'test-session': true })

    const { container } = renderChatBar({ sessionId: 'test-session' })
    const root = container.querySelector<HTMLElement>('[data-slot="composer-root"]')!
    expect(root).toBeTruthy()
    expect(root.hasAttribute('data-thread-scrolled-up')).toBe(true)
    expect(root.matches("[data-slot='composer-root'][data-thread-scrolled-up]")).toBe(true)

    const underlay = root.querySelector<HTMLElement>(':scope > .pointer-events-none')!
    expect(underlay).toBeTruthy()
    expect(underlay.matches("[data-slot='composer-root'] > .pointer-events-none")).toBe(true)
  })

  it('preserves backdrop blur styling when floating / popped out (data-popped-out)', () => {
    $composerPopout.set({ poppedOut: true, position: { bottom: 24, right: 24 } })

    const { container } = renderChatBar({ sessionId: 'test-session' })
    const root = container.querySelector<HTMLElement>('[data-slot="composer-root"]')!
    expect(root).toBeTruthy()
    expect(root.hasAttribute('data-popped-out')).toBe(true)
    expect(root.matches("[data-slot='composer-root'][data-popped-out]")).toBe(true)
  })

  it('preserves backdrop blur styling during and after streaming (busy state)', () => {
    const { container, rerender } = renderChatBar({ busy: true, sessionId: 'test-session' })
    let root = container.querySelector<HTMLElement>('[data-slot="composer-root"]')!
    expect(root.matches("[data-slot='composer-root']")).toBe(true)

    // Settle after streaming with scroll under composer
    $threadScrolledUpBySession.set({ 'test-session': true })
    rerender(
      <MemoryRouter>
        <I18nProvider configClient={null} initialLocale="en">
          <Runtime>
            <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, target: 'main' }}>
              <ComposerSurfaceProvider value="test-session">
                <ChatBar
                  busy={false}
                  cwd="/test"
                  disabled={false}
                  focusKey="test-session"
                  gateway={null}
                  maxRecordingSeconds={120}
                  onAddUrl={vi.fn()}
                  onAttachDroppedItems={vi.fn()}
                  onAttachImageBlob={vi.fn()}
                  onAttachPastedText={vi.fn()}
                  onCancel={vi.fn()}
                  onPasteClipboardImage={vi.fn()}
                  onPickFiles={vi.fn()}
                  onPickFolders={vi.fn()}
                  onPickImages={vi.fn()}
                  onRemoveAttachment={vi.fn()}
                  onSteer={vi.fn()}
                  onSteerHidden={vi.fn()}
                  onSubmit={vi.fn()}
                  onTranscribeAudio={vi.fn()}
                  queueSessionKey="test-session"
                  sessionId="test-session"
                  state={defaultChatBarState}
                />
              </ComposerSurfaceProvider>
            </ComposerScopeProvider>
          </Runtime>
        </I18nProvider>
      </MemoryRouter>
    )

    root = container.querySelector<HTMLElement>('[data-slot="composer-root"]')!
    expect(root.hasAttribute('data-thread-scrolled-up')).toBe(true)
    expect(root.matches("[data-slot='composer-root'][data-thread-scrolled-up]")).toBe(true)
    const underlay = root.querySelector<HTMLElement>(':scope > .pointer-events-none')!
    expect(underlay.matches("[data-slot='composer-root'] > .pointer-events-none")).toBe(true)
  })

  it('ChatBarFallback matches composer-root backdrop blur styling', () => {
    const { container } = render(<ChatBarFallback />)
    const root = container.querySelector<HTMLElement>('[data-slot="composer-root"]')!
    expect(root).toBeTruthy()
    expect(root.matches("[data-slot='composer-root']")).toBe(true)
  })
})
