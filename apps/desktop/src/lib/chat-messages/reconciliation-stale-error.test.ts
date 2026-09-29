import { describe, expect, it } from 'vitest'

import { preserveLocalAssistantErrors } from './reconciliation'
import type { ChatMessage } from './types'

const msg = (id: string, role: ChatMessage['role'], text: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id,
  role,
  parts: [{ type: 'text', text }] as ChatMessage['parts'],
  ...extra
})

// #46: an old errored turn with two images, Hermes' failed_turn row, newer turns
// and a peer-wake row. The windowed refresh starts at the failed_turn row.
const oldUser = msg('local-user', 'user', 'compare these', {
  attachmentRefs: ['@image:/tmp/one.png', '@image:/tmp/two.png'],
  timestamp: 1_000
})
const oldError = msg('local-err', 'assistant', '', { error: 'Claude CLI exited mid-turn', timestamp: 1_001 })
const failedTurn = msg('h-12', 'system', 'This turn failed.', { failedTurn: true, rowId: 12, timestamp: 1_002 })
const newer = [
  msg('h-20', 'user', 'next question', { rowId: 20, timestamp: 2_000 }),
  msg('h-21', 'assistant', 'next answer', { rowId: 21, timestamp: 2_001 }),
  msg('h-30', 'user', 'latest question', { rowId: 30, timestamp: 3_000 }),
  msg('h-31', 'assistant', 'latest answer', { rowId: 31, timestamp: 3_001 })
]
const peerWake = msg('h-40', 'system', 'woken by peer message: rex', { rowId: 40, timestamp: 4_000 })

describe('#46 stale local error run', () => {
  it('appears once, in place, and nothing lands after the newest row', () => {
    const current = [oldUser, oldError, failedTurn, ...newer, peerWake]
    const next = [failedTurn, ...newer, peerWake]

    const merged = preserveLocalAssistantErrors(next, current)

    expect(merged.at(-1)?.id).toBe('h-40')
    expect(merged.filter(message => message.error).length).toBeLessThanOrEqual(1)
    expect(merged.filter(message => message.id === 'local-user').length).toBeLessThanOrEqual(1)
    expect(merged.map(message => message.id).slice(-5)).toEqual(['h-20', 'h-21', 'h-30', 'h-31', 'h-40'])
  })

  it('a later committed reply alone makes the local error stale', () => {
    const current = [oldUser, oldError, ...newer, peerWake]
    const next = [...newer, peerWake]

    const merged = preserveLocalAssistantErrors(next, current)

    expect(merged.at(-1)?.id).toBe('h-40')
    expect(merged.some(message => message.id === 'local-err')).toBe(false)
  })

  it('keeps a still-current local error at the tail', () => {
    const current = [...newer, msg('local-u', 'user', 'now'), msg('local-e', 'assistant', '', { error: 'boom' })]
    const merged = preserveLocalAssistantErrors([...newer], current)

    expect(merged.at(-1)?.id).toBe('local-e')
  })
})
