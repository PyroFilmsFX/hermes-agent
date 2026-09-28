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
import {
  $composerForwardTargets,
  composerForwardTargetFor,
  setComposerForwardTarget,
  setComposerForwardTtl
} from '@/lib/owner-forward/composer-target'
import type { ComposerAttachment } from '@/store/composer'
import { $notifications, clearNotifications } from '@/store/notifications'
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
  $composerForwardTargets.set({})
})

afterEach(() => {
  cleanup()
  clearNotifications()
  setForwardGatewayRequestForTests(null)
  delete (window as any).hermesDesktop
})

function renderSubmit({
  attachments = [],
  busy = false,
  text = ''
}: { attachments?: ComposerAttachment[]; busy?: boolean; text?: string } = {}) {
  const draftRef = { current: text }
  const editor = window.document.createElement('div')
  editor.dataset.slot = 'composer-rich-input'
  editor.textContent = text
  const onSubmit = vi.fn(async () => true)
  const onSteer = vi.fn(async () => true)
  const queueCurrentDraft = vi.fn(() => true)
  const onCancel = vi.fn()
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
        busy,
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
        onCancel,
        onSteer,
        onSteerHidden: vi.fn(async () => true),
        onSubmit,
        queueCurrentDraft,
        queueEdit: null,
        queuedPrompts: [],
        sessionId: 'runtime-1',
        setComposerText: vi.fn(),
        stashAt: vi.fn()
      }),
    { wrapper: Wrapper }
  )

  return { clearDraft, draftRef, hook, loadIntoComposer, onCancel, onSteer, onSubmit, queueCurrentDraft, surfaceId }
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
    // D25: the forward text goes through secrets.mask before signing; clean text comes back unchanged.
    gatewayRequest.mockImplementation(async (method: string, params: { text?: string }) =>
      method === 'secrets.mask'
        ? { text: params.text }
        : { results: [{ target_session_id: 'worker_session_1', status: 'queued' }] }
    )
    const { clearDraft, hook, onSubmit } = renderSubmit({ text: '/to hermes:worker-one yes, merge\nafter CI' })

    act(() => hook.result.current.submitDraft())

    await vi.waitFor(() => expect(gatewayRequest).toHaveBeenCalledWith('owner.forward', expect.anything(), expect.anything()))
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
    expect(gatewayRequest).not.toHaveBeenCalledWith('owner.forward', expect.anything(), expect.anything())
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

describe('D29: the composer "send to" target', () => {
  const signed = {
    ok: true,
    decisionId: 'od_c',
    grantId: 'og_c',
    envelope: { format: 'hermes-owner-grant/v1', kid: 'k', payload: 'p', sig: 's' },
    targets: ['default:worker_session_1']
  }

  function maskThenDeliver(status = 'delivered') {
    gatewayRequest.mockImplementation(async (method: string, params: { text?: string }) =>
      method === 'secrets.mask'
        ? { text: params.text }
        : { results: [{ target_session_id: 'worker_session_1', status }] }
    )
  }

  function setTarget() {
    setComposerForwardTarget('mgr_session_1', {
      profile: 'default',
      session_id: 'worker_session_1',
      title: 'worker-one'
    })
  }

  it('a set target: Send masks, confirms in main, calls owner.forward, and never posts a turn here', async () => {
    confirm.mockResolvedValue(signed)
    maskThenDeliver()
    setTarget()
    setComposerForwardTtl('mgr_session_1', 3_600_000)
    const { clearDraft, hook, onSubmit } = renderSubmit({ text: '> quoted line\n\nplease pick this up' })

    act(() => hook.result.current.submitDraft())

    await vi.waitFor(() =>
      expect(gatewayRequest).toHaveBeenCalledWith('owner.forward', expect.anything(), expect.any(Number))
    )
    const methods = gatewayRequest.mock.calls.map(call => call[0])
    expect(methods.indexOf('secrets.mask')).toBeLessThan(methods.indexOf('owner.forward'))
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        gesture: 'slash_to',
        text: '> quoted line\n\nplease pick this up',
        origin: { session_id: 'mgr_session_1', message_id: null, role: 'user' },
        targets: [{ profile: 'default', session_id: 'worker_session_1' }],
        scope: [],
        ttlMs: 3_600_000
      })
    )
    expect(gatewayRequest).toHaveBeenCalledWith(
      'owner.forward',
      expect.objectContaining({ text: '> quoted line\n\nplease pick this up', targets: ['default:worker_session_1'] }),
      expect.any(Number)
    )
    expect(onSubmit).not.toHaveBeenCalled()
    expect(clearDraft).toHaveBeenCalled()
    // The sent confirmation names the target.
    await vi.waitFor(() =>
      expect($notifications.get().some(n => n.kind === 'success' && /Sent to worker-one/.test(n.message))).toBe(true)
    )
    // One-shot (D35): a delivered forward clears the target, so the next message sends here.
    expect(composerForwardTargetFor('mgr_session_1')).toBeNull()
  })

  it('a set target while this chat is busy still forwards: no steer, no queue', async () => {
    confirm.mockResolvedValue(signed)
    maskThenDeliver('queued')
    setTarget()
    const { hook, onSteer, onSubmit, queueCurrentDraft } = renderSubmit({ busy: true, text: 'for the worker' })

    act(() => hook.result.current.submitDraft())

    await vi.waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(onSteer).not.toHaveBeenCalled()
    expect(queueCurrentDraft).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('an empty composer keeps Stop while busy, even with a target set', () => {
    setTarget()
    const { hook, onCancel } = renderSubmit({ busy: true, text: '' })

    act(() => hook.result.current.submitDraft())

    expect(onCancel).toHaveBeenCalled()
    expect(confirm).not.toHaveBeenCalled()
  })

  it('⌘↩ / the queue button with a target set forwards instead of queueing here', async () => {
    confirm.mockResolvedValue({ ok: false, cancelled: true, reason: 'dialog' })
    maskThenDeliver()
    setTarget()
    const { hook, queueCurrentDraft } = renderSubmit({ busy: true, text: 'for the worker' })

    act(() => hook.result.current.queueDraft())

    await vi.waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(queueCurrentDraft).not.toHaveBeenCalled()
  })

  it('Cancel in the native dialog keeps the draft; nothing is sent anywhere', async () => {
    confirm.mockResolvedValue({ ok: false, cancelled: true, reason: 'dialog' })
    maskThenDeliver()
    setTarget()
    const { clearDraft, hook, onSubmit } = renderSubmit({ text: 'not yet' })

    act(() => hook.result.current.submitDraft())

    await vi.waitFor(() => expect(confirm).toHaveBeenCalled())
    await Promise.resolve()
    expect(clearDraft).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(gatewayRequest).not.toHaveBeenCalledWith('owner.forward', expect.anything(), expect.anything())
  })

  it('a set target refuses attachments and slash commands without sending', () => {
    setTarget()
    const attachment: ComposerAttachment = { id: 'a1', kind: 'file', label: 'pasted.txt' }
    const withFile = renderSubmit({ attachments: [attachment], text: 'see file' })

    act(() => withFile.hook.result.current.submitDraft())

    const slash = renderSubmit({ text: '/new' })

    act(() => slash.hook.result.current.submitDraft())

    expect(confirm).not.toHaveBeenCalled()
    expect(withFile.onSubmit).not.toHaveBeenCalled()
    expect(slash.onSubmit).not.toHaveBeenCalled()
    expect(slash.clearDraft).not.toHaveBeenCalled()
  })

  it('a cleared target restores normal send', async () => {
    setTarget()
    setComposerForwardTarget('mgr_session_1', null)
    const { hook, onSubmit } = renderSubmit({ text: 'back to this chat' })

    act(() => hook.result.current.submitDraft())

    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith('back to this chat', expect.anything()))
    expect(confirm).not.toHaveBeenCalled()
    expect(gatewayRequest).not.toHaveBeenCalled()
  })

  it('a target set for ANOTHER chat does not re-route this one', async () => {
    setComposerForwardTarget('worker_session_1', { profile: 'default', session_id: 'mgr_session_1', title: 'manager' })
    const { hook, onSubmit } = renderSubmit({ text: 'normal' })

    act(() => hook.result.current.submitDraft())

    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(confirm).not.toHaveBeenCalled()
  })
})
