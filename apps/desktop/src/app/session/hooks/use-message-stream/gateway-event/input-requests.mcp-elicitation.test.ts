import { afterEach, describe, expect, it, vi } from 'vitest'

import { $mcpElicitations, resetMcpElicitationsForTests } from '@/store/mcp-elicitation'
import { dispatchNativeNotification } from '@/store/native-notifications'

import { handleInputRequestEvent } from './input-requests'
import type { GatewayEventContext } from './types'

vi.mock('@/store/native-notifications', () => ({ dispatchNativeNotification: vi.fn() }))

function elicitation(
  payload: Record<string, unknown>,
  { ambientSession = 'chat-a', explicitSid = '' }: { ambientSession?: null | string; explicitSid?: string } = {}
): GatewayEventContext {
  return {
    deps: { updateSessionState: vi.fn(), upsertToolCall: vi.fn() } as unknown as GatewayEventContext['deps'],
    event: { payload, profile: 'default', type: 'mcp.elicitation.request' } as GatewayEventContext['event'],
    explicitSid,
    fromActiveSource: () => true,
    isActiveEvent: true,
    occurredAt: 1,
    payload: payload as GatewayEventContext['payload'],
    scheduleConfigRefresh: vi.fn(),
    sessionId: explicitSid || ambientSession
  }
}

describe('mcp.elicitation.request', () => {
  afterEach(() => {
    resetMcpElicitationsForTests()
    vi.clearAllMocks()
  })

  it('queues the session-less broadcast app-level, not under the chat that happened to be open', () => {
    const payload = {
      request_id: 'el-1',
      server: 'billing',
      message: 'Who pays?',
      mode: 'form',
      requestedSchema: { type: 'object', properties: {} }
    }

    expect(handleInputRequestEvent(elicitation(payload))).toBe(true)
    expect($mcpElicitations.get()).toEqual([
      expect.objectContaining({
        requestId: 'el-1',
        server: 'billing',
        mode: 'form',
        sessionId: null,
        profile: 'default'
      })
    ])
    expect(dispatchNativeNotification).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'input', global: true, title: 'billing needs a few details' })
    )
  })

  it('keeps an explicit session id when the gateway names one', () => {
    handleInputRequestEvent(
      elicitation(
        { request_id: 'u-1', server: 'gh', message: '', mode: 'url', url: 'https://x.example' },
        {
          explicitSid: 'chat-b'
        }
      )
    )

    expect($mcpElicitations.get()[0]).toMatchObject({ requestId: 'u-1', mode: 'url', sessionId: 'chat-b' })
  })

  it('consumes and drops a malformed payload', () => {
    expect(handleInputRequestEvent(elicitation({ server: 'x' }))).toBe(true)
    expect($mcpElicitations.get()).toEqual([])
    expect(dispatchNativeNotification).not.toHaveBeenCalled()
  })
})
