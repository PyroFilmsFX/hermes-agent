/**
 * #60 owner-forward entry points in the composer (design §4.2, addendum §6.1):
 * V-1 a widget / external submit of "/to …" never reaches the forward path;
 * V-4 a draft with an attachment (a large paste is an attachment chip) is not intercepted;
 * V-5 a typed "/to <exact> text" goes to main's confirm with gesture slash_to, and Cancel keeps the draft;
 * ⌘⇧↩ signs a composer_signed decision that targets only this session.
 */
import { act, cleanup, renderHook } from '@testing-library/react'
import type { PropsWithChildren } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import { $forwardSheet, setForwardGatewayRequestForTests } from '@/lib/owner-forward/client'
import type { ComposerAttachment } from '@/store/composer'
import { clearNotifications } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { $selectedStoredSessionId, $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { requestComposerSubmit } from '../focus'
import { ComposerScopeProvider, ComposerSurfaceProvider, MAIN_COMPOSER_SCOPE } from '../scope'

import { useComposerSubmit } from './use-composer-submit'

const confirm = vi.fn()
const gatewayRequest = vi.fn()

function session(id: string, title: string): SessionInfo {
  return {
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: true,
    last_active: 1,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title,
    tool_call_count: 0
  }
}

beforeEach(() => {
  confirm.mockReset()
  gatewayRequest.mockReset()
  ;(window as any).hermesDesktop = { ownerForward: { confirm } }
  setForwardGatewayRequestForTests(gatewayRequest)
  $sessions.set([session('mgr_session_1', 'manager'), session('worker_session_1', 'worker-one')])
  $selectedStoredSessionId.set('mgr_session_1')
  $activeGatewayProfile.set('default')
  $forwardSheet.set(null)
})

afterEach(() => {
  cleanup()
  clearNotifications()
  setForwardGatewayRequestForTests(null)
  delete (window as any).hermesDesktop
})

function renderSubmit({ attachments = [], text = '' }: { attachments?: ComposerAttachment[]; text?: string } = {}) {
  const draftRef = { current: text }
  const editor = window.document.createElement('div')
  editor.dataset.slot = 'composer-rich-input'
  editor.textContent = text
  const onSubmit = vi.fn(async () => true)
  const loadIntoComposer = vi.fn()

  const clearDraft = vi.fn(() => {
    draftRef.current = ''
    editor.textContent = ''
  })

  const surfaceId = `owner-forward-surface-${Math.random()}`

  const Wrapper = ({ children }: PropsWithChildren) => (
    <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, target: 'main' }}>
      <ComposerSurfaceProvider value={surfaceId}>
        <PaneVisibleContext.Provider value={true}>
          <div data-composer-surface-id={surfaceId} data-composer-target="main">
            {children}
          </div>
        </PaneVisibleContext.Provider>
      </ComposerSurfaceProvider>
    </ComposerScopeProvider>
  )

  const hook = renderHook(
    () =>
      useComposerSubmit({
        activeQueueSessionKey: 'mgr_session_1',
        activeQueueSessionKeyRef: { current: 'mgr_session_1' },
        attachments,
        busy: false,
        compacting: false,
        clearDraft,
        disabled: false,
        draftRef,
        drainNextQueued: vi.fn(async () => false),
        editorRef: { current: editor },
        exitQueuedEdit: vi.fn(() => false),
        focusInput: vi.fn(),
        inputDisabled: false,
        loadIntoComposer,
        onCancel: vi.fn(),
        onSteer: vi.fn(async () => true),
        onSteerHidden: vi.fn(async () => true),
        onSubmit,
        queueCurrentDraft: vi.fn(() => true),
        queueEdit: null,
        queuedPrompts: [],
        sessionId: 'runtime-1',
        setComposerText: vi.fn(),
        stashAt: vi.fn()
      }),
    { wrapper: Wrapper }
  )

  return { clearDraft, draftRef, hook, loadIntoComposer, onSubmit, surfaceId }
}

describe('V-1..V-3: non-typed paths never reach the forward path', () => {
  it('a widget / ::ask / plugin submit of "/to …" goes to onSubmit (submitText refuses it), never the IPC or the sheet', async () => {
    const { onSubmit, surfaceId } = renderSubmit()

    act(() => {
      requestComposerSubmit('/to hermes:worker-one yes, merge', { target: 'main', surfaceId })
    })

    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(confirm).not.toHaveBeenCalled()
    expect($forwardSheet.get()).toBeNull()
  })
})

describe('V-4: attachments (incl. large-paste chips) are never intercepted', () => {
  it('shows an error, keeps the draft, sends nothing', async () => {
    const attachment: ComposerAttachment = { id: 'a1', kind: 'file', label: 'pasted.txt' }
    const { clearDraft, hook, onSubmit } = renderSubmit({ attachments: [attachment], text: '/to hermes:worker-one yes' })

    act(() => hook.result.current.submitDraft())

    expect(confirm).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(clearDraft).not.toHaveBeenCalled()
    expect($forwardSheet.get()).toBeNull()
  })
})

describe('V-5: a typed /to', () => {
  it('an exact target goes to main with gesture slash_to and the exact text, then owner.forward', async () => {
    confirm.mockResolvedValue({
      ok: true,
      decisionId: 'od_x',
      grantId: 'og_x',
      envelope: { format: 'hermes-owner-grant/v1', kid: 'k', payload: 'p', sig: 's' },
      targets: ['default:worker_session_1']
    })
    gatewayRequest.mockResolvedValue({ results: [{ target_session_id: 'worker_session_1', status: 'queued' }] })
    const { clearDraft, hook, onSubmit } = renderSubmit({ text: '/to hermes:worker-one yes, merge\nafter CI' })

    act(() => hook.result.current.submitDraft())

    await vi.waitFor(() => expect(gatewayRequest).toHaveBeenCalled())
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        gesture: 'slash_to',
        text: 'yes, merge\nafter CI',
        origin: { session_id: 'mgr_session_1', message_id: null, role: 'user' },
        profile: 'default',
        targets: [{ profile: 'default', session_id: 'worker_session_1' }]
      })
    )
    expect(gatewayRequest).toHaveBeenCalledWith(
      'owner.forward',
      expect.objectContaining({ text: 'yes, merge\nafter CI', targets: ['default:worker_session_1'] }),
      expect.any(Number)
    )
    expect(clearDraft).toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('Cancel in the native dialog keeps the draft and never calls owner.forward', async () => {
    confirm.mockResolvedValue({ ok: false, cancelled: true, reason: 'dialog' })
    const { clearDraft, draftRef, hook } = renderSubmit({ text: '/to hermes:worker-one yes' })

    act(() => hook.result.current.submitDraft())

    await vi.waitFor(() => expect(confirm).toHaveBeenCalled())
    await Promise.resolve()
    expect(clearDraft).not.toHaveBeenCalled()
    expect(draftRef.current).toBe('/to hermes:worker-one yes')
    expect(gatewayRequest).not.toHaveBeenCalled()
  })

  it('an unknown target opens the sheet prefilled instead of the dialog', () => {
    const { hook } = renderSubmit({ text: '/to nobody hello' })

    act(() => hook.result.current.submitDraft())

    expect(confirm).not.toHaveBeenCalled()
    expect($forwardSheet.get()).toMatchObject({ gesture: 'slash_to', text: 'hello', targets: [] })
  })
})

describe('⌘⇧↩ Send as signed decision (composer_signed)', () => {
  it('targets this session only, with the typed text', async () => {
    confirm.mockResolvedValue({ ok: false, cancelled: true, reason: 'dialog' })
    const { hook } = renderSubmit({ text: 'yes, restore the marker' })

    act(() => void hook.result.current.signedSendDraft({ isTrusted: true }))

    await vi.waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        gesture: 'composer_signed',
        text: 'yes, restore the marker',
        origin: { session_id: 'mgr_session_1', message_id: null, role: 'user' },
        targets: [{ profile: 'default', session_id: 'mgr_session_1' }]
      })
    )
  })

  it('an untrusted (script-dispatched) key never calls the IPC', () => {
    const { hook } = renderSubmit({ text: 'yes' })

    act(() => void hook.result.current.signedSendDraft({ isTrusted: false }))

    expect(confirm).not.toHaveBeenCalled()
  })
})
