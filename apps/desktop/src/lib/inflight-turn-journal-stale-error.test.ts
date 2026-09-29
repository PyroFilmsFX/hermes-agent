import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'

import { mergeInFlightMessages } from './inflight-turn-journal'

const msg = (id: string, role: ChatMessage['role'], text: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id,
  role,
  parts: [{ type: 'text', text }] as ChatMessage['parts'],
  ...extra
})

describe('#46 journal error run', () => {
  const base = [
    msg('h-10', 'user', 'compare these', { attachmentRefs: ['@image:/stored/one.png', '@image:/stored/two.png'], rowId: 10, timestamp: 1_000 }),
    msg('h-12', 'system', 'This turn failed.', { failedTurn: true, rowId: 12, timestamp: 1_002 }),
    msg('h-20', 'user', 'next question', { rowId: 20, timestamp: 2_000 }),
    msg('h-21', 'assistant', 'next answer', { rowId: 21, timestamp: 2_001 }),
    msg('h-40', 'system', 'woken by peer message: rex', { rowId: 40, timestamp: 4_000 })
  ]

  it('a failed_turn row for the prompt retires the journaled error (no tail append)', () => {
    const tail = [
      msg('j-u', 'user', 'compare these', { attachmentRefs: ['@image:/tmp/one.png', '@image:/tmp/two.png'], timestamp: 1_000 }),
      msg('j-e', 'assistant', '', { error: 'Claude CLI exited mid-turn', timestamp: 1_001 })
    ]

    const result = mergeInFlightMessages(base, tail)

    expect(result.applied).toBe(false)
    expect(result.messages.at(-1)?.id).toBe('h-40')
  })

  it('an error run older than the latest committed user row is stale', () => {
    const tail = [msg('j-e', 'assistant', '', { error: 'boom', timestamp: 1_500 })]
    const result = mergeInFlightMessages(base.filter(message => !message.failedTurn), tail)

    expect(result.applied).toBe(false)
  })
})
