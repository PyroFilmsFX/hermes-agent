import { describe, expect, it } from 'vitest'

import { preserveLocalAssistantErrors } from './reconciliation'
import type { ChatMessage } from './types'

describe('preserveLocalAssistantErrors tool result reconciliation', () => {
  it('keeps a live result when refreshed history has the matching call but no result row', () => {
    const live: ChatMessage[] = [
      {
        id: 'live-assistant',
        role: 'assistant',
        parts: [
          {
            type: 'tool-call',
            toolCallId: 'tool-use-1',
            toolName: 'Bash',
            args: { command: 'ls' },
            argsText: '{"command":"ls"}',
            result: 'file.txt',
            completedAt: 12,
            isError: false,
            toolResultMetadata: { summary: '1 file' }
          }
        ]
      }
    ]
    const refreshed: ChatMessage[] = [
      {
        id: 'stored-assistant',
        role: 'assistant',
        parts: [
          {
            type: 'tool-call',
            toolCallId: 'tool-use-1',
            toolName: 'Bash',
            args: { command: 'ls' },
            argsText: '{"command":"ls"}'
          }
        ]
      }
    ]

    const [message] = preserveLocalAssistantErrors(refreshed, live)

    expect(message?.parts[0]).toMatchObject({
      type: 'tool-call',
      result: 'file.txt',
      completedAt: 12,
      isError: false,
      toolResultMetadata: { summary: '1 file' }
    })
  })

  it('keeps the stored result when history already has one', () => {
    const live: ChatMessage[] = [
      {
        id: 'live-assistant',
        role: 'assistant',
        parts: [
          {
            type: 'tool-call',
            toolCallId: 'tool-use-1',
            toolName: 'Bash',
            args: {},
            argsText: '',
            result: 'live result'
          }
        ]
      }
    ]
    const refreshed: ChatMessage[] = [
      {
        id: 'stored-assistant',
        role: 'assistant',
        parts: [
          {
            type: 'tool-call',
            toolCallId: 'tool-use-1',
            toolName: 'Bash',
            args: {},
            argsText: '',
            result: 'stored result'
          }
        ]
      }
    ]

    const [message] = preserveLocalAssistantErrors(refreshed, live)

    expect(message?.parts[0]).toMatchObject({ type: 'tool-call', result: 'stored result' })
  })
})
