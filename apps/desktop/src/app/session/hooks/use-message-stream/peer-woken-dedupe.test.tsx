import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { preserveLocalPendingTurnMessages } from '@/app/session/hooks/use-session-actions/utils'
import type { ChatMessage } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'

import { renderMessageStream } from './test-harness'

const SID = 'rt-peer-woken'
const STORED = 'stored-peer-woken'

function textMsg(id: string, role: ChatMessage['role'], text: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id,
    role,
    parts: [{ type: 'text', text }],
    ...extra
  }
}

describe('peer-woken turns render exactly once under their header (#U1.10)', () => {
  afterEach(() => {
    cleanup()
  })

  it('renders exactly one assistant copy under its header both live and after hydration refresh', () => {
    const deliveryId = 'deliv-peer-12345'
    const states = new Map([[SID, createClientSessionState(STORED)]])
    const stream = renderMessageStream(SID, { states })

    // 1. Peer header emitted before stream deltas: lifecycle row + peer-message row + assistant stream start
    act(() => {
      stream.handleEvent({
        session_id: SID,
        type: 'message.start',
        payload: {
          background: true,
          display_kind: 'session_lifecycle',
          delivery_id: deliveryId
        }
      })
      stream.handleEvent({
        session_id: SID,
        type: 'message.complete',
        payload: {
          background: true,
          display_kind: 'session_lifecycle',
          delivery_id: deliveryId,
          text: 'woken by peer message: hermes',
          display_metadata: { event: 'woken', source: 'peer', by: 'hermes', delivery_id: deliveryId }
        }
      })
      stream.handleEvent({
        session_id: SID,
        type: 'message.start',
        payload: {
          background: true,
          display_kind: 'peer_message',
          delivery_id: deliveryId
        }
      })
      stream.handleEvent({
        session_id: SID,
        type: 'message.complete',
        payload: {
          background: true,
          display_kind: 'peer_message',
          delivery_id: deliveryId,
          text: 'please check the system status',
          display_metadata: { direction: 'in', peer: 'hermes', delivery_id: deliveryId }
        }
      })
      // Assistant stream opens under the header carrying delivery_id
      stream.handleEvent({
        session_id: SID,
        type: 'message.start',
        payload: {
          background: true,
          delivery_id: deliveryId
        }
      })
    })

    // 2. Stream assistant deltas into this new turn
    act(() => {
      stream.appendDelta(SID, 'System status: all healthy.')
    })

    // 3. Complete assistant message carrying delivery_id
    act(() => {
      stream.handleEvent({
        session_id: SID,
        type: 'message.complete',
        payload: {
          background: true,
          delivery_id: deliveryId,
          text: 'System status: all healthy.'
        }
      })
    })

    const localMessages = states.get(SID)!.messages
    // Live view: lifecycle (system) + peer_message (system) + assistant reply
    expect(localMessages).toHaveLength(3)
    const assistantLive = localMessages.filter(m => m.role === 'assistant')
    expect(assistantLive).toHaveLength(1)
    expect(assistantLive[0].deliveryId).toBe(deliveryId)

    // 4. Hydration / history refresh from DB:
    // The DB returns the persisted rows carrying delivery_id
    const hydratedAuthoritative: ChatMessage[] = [
      textMsg(`db-life-${deliveryId}`, 'system', 'woken by peer message: hermes', { deliveryId }),
      textMsg(`db-peer-${deliveryId}`, 'system', 'please check the system status', { deliveryId }),
      textMsg(`db-reply-${deliveryId}`, 'assistant', 'System status: all healthy.', { deliveryId })
    ]
    const reconciled = preserveLocalPendingTurnMessages(hydratedAuthoritative, localMessages)

    // Must reconcile to EXACTLY 3 messages: 1 lifecycle + 1 peer + 1 assistant reply.
    // Must NOT duplicate to 4 or more messages!
    expect(reconciled).toHaveLength(3)
    const assistantReconciled = reconciled.filter(m => m.role === 'assistant')
    expect(assistantReconciled).toHaveLength(1)
    expect(assistantReconciled[0].parts[0]).toMatchObject({ text: 'System status: all healthy.' })
  })
})
