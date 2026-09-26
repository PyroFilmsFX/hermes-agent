import { beforeEach, describe, expect, it, vi } from 'vitest'

const { refreshSupportedSessionControlAfterTurn } = vi.hoisted(() => ({
  refreshSupportedSessionControlAfterTurn: vi.fn(async () => undefined)
}))

const { clearActiveSessionTodos, clearAllPrompts, clearClarifyRequest, playCompletionSound } = vi.hoisted(() => ({
  clearActiveSessionTodos: vi.fn(),
  clearAllPrompts: vi.fn(),
  clearClarifyRequest: vi.fn(),
  playCompletionSound: vi.fn()
}))

vi.mock('@/store/session-control', () => ({ refreshSupportedSessionControlAfterTurn }))
vi.mock('@/store/prompts', () => ({ clearAllPrompts }))
vi.mock('@/store/todos', () => ({ clearActiveSessionTodos }))
vi.mock('@/store/clarify', () => ({ clearClarifyRequest }))
vi.mock('@/lib/completion-sound', () => ({ playCompletionSound }))

beforeEach(() => {
  vi.clearAllMocks()
})

import type { GatewayEventName } from '@hermes/shared'

import type { ChatMessage } from '@/lib/chat-messages'

import { handleMessageStreamEvent } from './message-stream'
import type { GatewayEventContext } from './types'

function context(type: GatewayEventName): GatewayEventContext {
  return {
    deps: {
      activeGatewayProfile: 'default',
      activeSessionIdRef: { current: 's1' },
      appendAssistantDelta: vi.fn(),
      appendReasoningDelta: vi.fn(),
      compactedTurnRef: { current: new Set() },
      completeAssistantMessage: vi.fn(),
      failAssistantMessage: vi.fn(),
      finalizeInterimAssistantMessage: vi.fn(),
      flushQueuedDeltas: vi.fn(),
      hydrateFromStoredSession: vi.fn(async () => undefined),
      lastCwdInfoSessionRef: { current: null },
      nativeSubagentSessionsRef: { current: new Set() },
      queryClient: {} as GatewayEventContext['deps']['queryClient'],
      refreshHermesConfig: vi.fn(async () => undefined),
      scheduleSessionsRefresh: vi.fn(),
      sessionInterrupted: vi.fn(() => false),
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn(),
      upsertToolCall: vi.fn()
    },
    event: { type },
    explicitSid: 's1',
    fromActiveSource: () => true,
    isActiveEvent: false,
    occurredAt: 1_700_000_100,
    payload: { text: 'completed' },
    scheduleConfigRefresh: vi.fn(),
    sessionId: 's1'
  }
}

describe('handleMessageStreamEvent session-control integration', () => {
  it('refreshes only after message.complete, through the store seam', () => {
    expect(handleMessageStreamEvent(context('message.delta'))).toBe(true)
    expect(refreshSupportedSessionControlAfterTurn).not.toHaveBeenCalled()

    expect(handleMessageStreamEvent(context('message.complete'))).toBe(true)
    expect(refreshSupportedSessionControlAfterTurn).toHaveBeenCalledTimes(1)
    expect(refreshSupportedSessionControlAfterTurn).toHaveBeenCalledWith('s1')
  })
})

describe('handleMessageStreamEvent background delivery contracts', () => {
  it('does not tear down foreground state or play completion sound for background complete', () => {
    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'peer_message',
      text: 'Background peer result',
      display_metadata: { direction: 'in', peer: 'planner' }
    } as unknown as GatewayEventContext['payload']

    handleMessageStreamEvent(ctx)

    expect(clearActiveSessionTodos).not.toHaveBeenCalled()
    expect(clearAllPrompts).not.toHaveBeenCalled()
    expect(playCompletionSound).not.toHaveBeenCalled()
  })

  it('tears down foreground state and plays completion sound for foreground complete', () => {
    const ctx = context('message.complete')

    handleMessageStreamEvent(ctx)

    expect(clearActiveSessionTodos).toHaveBeenCalledWith('s1')
    expect(clearAllPrompts).toHaveBeenCalledWith('s1')
    expect(playCompletionSound).toHaveBeenCalledWith('s1')
  })

  it('leaves busy false and is not refused when interrupted on background message.start', () => {
    const ctx = context('message.start')
    ctx.payload = { background: true, display_kind: 'peer_message' } as unknown as GatewayEventContext['payload']

    let currentState = {
      busy: false,
      awaitingResponse: false,
      interrupted: true,
      messages: [],
      storedSessionId: 's1',
      streamId: null
    } as unknown as ReturnType<GatewayEventContext['deps']['updateSessionState']>

    ctx.deps.updateSessionState = vi.fn((_sid, updater) => {
      currentState = updater(currentState)

      return currentState
    })

    const handled = handleMessageStreamEvent(ctx)
    expect(handled).toBe(true)
    expect(currentState.busy).toBe(false)
    expect(currentState.awaitingResponse).toBe(false)
    expect(currentState.interrupted).toBe(true)
    expect(currentState.streamId).toBeTruthy()
  })

  it('appends background complete while interrupted (not dropped) and does not re-arm busy', () => {
    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'peer_message',
      text: 'Inbound peer task instructions\nLine 2',
      display_metadata: {
        direction: 'in',
        peer: 'planner'
      }
    } as unknown as GatewayEventContext['payload']

    let currentState = {
      busy: false,
      awaitingResponse: false,
      interrupted: true,
      messages: [],
      storedSessionId: 's1',
      streamId: null
    } as unknown as ReturnType<GatewayEventContext['deps']['updateSessionState']>

    ctx.deps.updateSessionState = vi.fn((_sid, updater) => {
      currentState = updater(currentState)

      return currentState
    })

    const handled = handleMessageStreamEvent(ctx)
    expect(handled).toBe(true)
    expect(ctx.deps.completeAssistantMessage).not.toHaveBeenCalled()
    expect(currentState.busy).toBe(false)
    expect(currentState.interrupted).toBe(true)
    expect(currentState.messages).toHaveLength(1)

    const appended = currentState.messages[0]
    expect(appended.role).toBe('system')
    expect((appended.parts[0] as { text?: string })?.text).toContain('↘ from planner')
    expect(appended.asyncResult).toBe('Inbound peer task instructions\nLine 2')
  })

  it('appends sdk_background_result as assistant message without dropping when interrupted', () => {
    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'sdk_background_result',
      text: 'Background execution output'
    } as unknown as GatewayEventContext['payload']

    let currentState = {
      busy: false,
      awaitingResponse: false,
      interrupted: true,
      messages: [],
      storedSessionId: 's1',
      streamId: null
    } as unknown as ReturnType<GatewayEventContext['deps']['updateSessionState']>

    ctx.deps.updateSessionState = vi.fn((_sid, updater) => {
      currentState = updater(currentState)

      return currentState
    })

    const handled = handleMessageStreamEvent(ctx)
    expect(handled).toBe(true)
    expect(currentState.messages).toHaveLength(1)
    const appended = currentState.messages[0]
    expect(appended.role).toBe('assistant')
    expect((appended.parts[0] as { text?: string })?.text).toBe('Background execution output')
  })

  it('marks unread-finished for non-focused session on background complete', async () => {
    const { $unreadFinishedSessionIds, $selectedStoredSessionId } = await import('@/store/session')

    $unreadFinishedSessionIds.set([])
    $selectedStoredSessionId.set('other-session')

    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'peer_message',
      text: 'New update',
      display_metadata: { direction: 'in', peer: 'worker' }
    } as unknown as GatewayEventContext['payload']

    handleMessageStreamEvent(ctx)
    expect($unreadFinishedSessionIds.get()).toContain('s1')

    // But when the session is focused, it does not mark unread
    $unreadFinishedSessionIds.set([])
    $selectedStoredSessionId.set('s1')

    handleMessageStreamEvent(ctx)
    expect($unreadFinishedSessionIds.get()).not.toContain('s1')
  })

  it('updates a queued peer message status when peer_mailbox.settled arrives', async () => {
    const { $messages } = await import('@/store/session')
    const ctx = context('peer_mailbox.settled' as GatewayEventName)
    ctx.event = {
      type: 'peer_mailbox.settled',
      payload: { msg_id: 'msg-settle-100', status: 'delivered-live', attempts: 1 }
    } as unknown as GatewayEventContext['event']

    const queuedPeerMessage: ChatMessage = {
      id: 'p-1',
      role: 'system',
      parts: [{ type: 'text', text: '↗ to worker: deploy' }],
      asyncResult: 'deploy',
      peerMetadata: {
        msg_id: 'msg-settle-100',
        direction: 'out',
        peer: 'worker',
        status: 'queued',
        attempts: 1
      }
    }

    let sessionState = {
      busy: false,
      awaitingResponse: false,
      interrupted: false,
      messages: [queuedPeerMessage],
      storedSessionId: 's1',
      streamId: null
    } as unknown as ReturnType<GatewayEventContext['deps']['updateSessionState']>

    ctx.deps.sessionStateByRuntimeIdRef.current.set('s1', sessionState)
    ctx.deps.updateSessionState = vi.fn((_sid, updater) => {
      sessionState = updater(sessionState)

      return sessionState
    })

    $messages.set([queuedPeerMessage])

    const handled = handleMessageStreamEvent(ctx)
    expect(handled).toBe(true)

    // Checked in session state
    expect(sessionState.messages[0].peerMetadata?.status).toBe('delivered-live')
    expect(sessionState.messages[0].peerMetadata?.attempts).toBe(1)

    // Checked in $messages store
    expect($messages.get()[0].peerMetadata?.status).toBe('delivered-live')
  })

  it('populates peerMetadata on peer_message completion', () => {
    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'peer_message',
      text: 'Outbound command body',
      display_metadata: {
        direction: 'out',
        to: 'worker-node',
        msg_id: 'out-42',
        status: 'queued',
        attempts: 2
      }
    } as unknown as GatewayEventContext['payload']

    let currentState = {
      busy: false,
      awaitingResponse: false,
      interrupted: false,
      messages: [],
      storedSessionId: 's1',
      streamId: null
    } as unknown as ReturnType<GatewayEventContext['deps']['updateSessionState']>

    ctx.deps.updateSessionState = vi.fn((_sid, updater) => {
      currentState = updater(currentState)

      return currentState
    })

    const handled = handleMessageStreamEvent(ctx)
    expect(handled).toBe(true)
    expect(currentState.messages).toHaveLength(1)

    const msg = currentState.messages[0]
    expect(msg.role).toBe('system')
    expect(msg.peerMetadata).toEqual({
      direction: 'out',
      peer: 'worker-node',
      from: undefined,
      from_session_id: undefined,
      to: 'worker-node',
      msg_id: 'out-42',
      via: undefined,
      status: 'queued',
      attempts: 2
    })
  })

  it('uses from-name for the live peer card and keeps sender id as metadata', () => {
    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'peer_message',
      text: 'Inbound message',
      display_metadata: {
        direction: 'in',
        peer: 'hermes-session:sess-9',
        from_name: 'manager',
        from_session_id: 'sess-9'
      }
    } as unknown as GatewayEventContext['payload']
    let currentState = { messages: [], streamId: null } as unknown as ReturnType<
      GatewayEventContext['deps']['updateSessionState']
    >
    ctx.deps.updateSessionState = vi.fn((_sid, updater) => (currentState = updater(currentState)))

    handleMessageStreamEvent(ctx)

    expect(currentState.messages[0].parts[0]).toMatchObject({ text: expect.stringContaining('↘ from manager') })
    expect(currentState.messages[0].peerMetadata).toMatchObject({ peer: 'manager', from_session_id: 'sess-9' })
  })

  it('keeps live woken lifecycle metadata out of the body', () => {
    const ctx = context('message.complete')
    ctx.payload = {
      background: true,
      display_kind: 'session_lifecycle',
      display_metadata: { event: 'woken', source: 'peer-mailbox', by: 'manager', uuid: 'abc', delivery_id: 'd-1' }
    } as unknown as GatewayEventContext['payload']
    let currentState = { messages: [], streamId: null } as unknown as ReturnType<
      GatewayEventContext['deps']['updateSessionState']
    >
    ctx.deps.updateSessionState = vi.fn((_sid, updater) => (currentState = updater(currentState)))

    handleMessageStreamEvent(ctx)

    expect(currentState.messages[0].parts[0]).toMatchObject({ text: 'woken by peer message: manager' })
    expect(currentState.messages[0].asyncResult).toBeUndefined()
  })
})
