import { describe, expect, it } from 'vitest'

import type { SessionMessage } from '@/types/hermes'

import { formatShortTime, toChatMessages } from './hydration'
import type { ChatMessage } from './types'

function getText(msg: ChatMessage | undefined): string | undefined {
  const part = msg?.parts[0]

  return part && 'text' in part ? (part.text as string) : undefined
}

describe('hydration peer_message support', () => {
  it('keeps an outbound peer_message separate after assistant tool-call/result rows', () => {
    const messages = toChatMessages([
      {
        role: 'assistant',
        content: 'Sending the update now.',
        timestamp: 1_700_000_000
      },
      {
        role: 'assistant',
        content: '',
        tool_calls: [
          {
            id: 'send-1',
            function: { name: 'SendMessage', arguments: '{"peer":"bob","text":"Update sent"}' }
          }
        ],
        timestamp: 1_700_000_001
      },
      {
        role: 'tool',
        content: 'sent',
        tool_call_id: 'send-1',
        tool_name: 'SendMessage',
        timestamp: 1_700_000_002
      },
      {
        role: 'assistant',
        content: 'Full outbound peer body',
        display_kind: 'peer_message',
        display_metadata: { direction: 'out', peer: 'bob' },
        timestamp: 1_700_000_003
      }
    ] satisfies SessionMessage[])

    expect(messages).toHaveLength(2)
    expect(messages[0]?.role).toBe('assistant')
    expect(messages[0]?.parts.some(part => part.type === 'tool-call')).toBe(true)
    expect(messages[1]?.role).toBe('system')
    expect(messages[1]?.asyncResult).toBe('Full outbound peer body')
  })

  it('hydrates inbound peer_message to system-role scaffold row with time label and body', () => {
    const timestamp = 1_700_000_000
    const content = 'Hello from peer!\nHere is the detailed body.'

    const row: SessionMessage = {
      role: 'user',
      content,
      display_kind: 'peer_message',
      display_metadata: {
        direction: 'in',
        peer: 'alice'
      },
      timestamp
    }

    const messages = toChatMessages([row])
    expect(messages).toHaveLength(1)
    const msg = messages[0]

    expect(msg.role).toBe('system')
    const expectedTime = formatShortTime(timestamp)
    expect(msg.parts[0]?.type).toBe('text')
    expect(getText(msg)).toBe(`↘ from alice · ${expectedTime}`)
    expect(msg.asyncResult).toBe(content)
  })

  it('hydrates outbound peer_message to system-role scaffold row with ellipsized first line and body', () => {
    const content = 'Deploying database migration\nRunning step 1 of 4\nDone.'

    const row: SessionMessage = {
      role: 'assistant',
      content,
      display_kind: 'peer_message',
      display_metadata: {
        direction: 'out',
        peer: 'bob'
      },
      timestamp: 1_700_000_100
    }

    const messages = toChatMessages([row])
    expect(messages).toHaveLength(1)
    const msg = messages[0]

    expect(msg.role).toBe('system')
    expect(getText(msg)).toBe('↗ to bob: Deploying database migration…')
    expect(msg.asyncResult).toBe(content)
  })

  it('keeps short single-line outbound messages unellipsized', () => {
    const row: SessionMessage = {
      role: 'assistant',
      content: 'ping',
      display_kind: 'peer_message',
      display_metadata: {
        direction: 'out',
        peer: 'bob'
      },
      timestamp: 1_700_000_100
    }

    const messages = toChatMessages([row])
    expect(getText(messages[0])).toBe('↗ to bob: ping')
    expect(messages[0]?.asyncResult).toBe('ping')
  })

  it('ellipsizes long single-line outbound messages past 80 chars', () => {
    const longText = 'A'.repeat(100)

    const row: SessionMessage = {
      role: 'assistant',
      content: longText,
      display_kind: 'peer_message',
      display_metadata: {
        direction: 'out',
        peer: 'worker'
      }
    }

    const messages = toChatMessages([row])
    expect(getText(messages[0])).toBe(`↗ to worker: ${'A'.repeat(80)}…`)
    expect(messages[0]?.asyncResult).toBe(longText)
  })

  it('parses raw JSON string display_metadata gracefully', () => {
    const row: SessionMessage = {
      role: 'user',
      content: 'Inbound message',
      display_kind: 'peer_message',
      display_metadata: JSON.stringify({
        direction: 'in',
        peer: 'charlie'
      }),
      timestamp: 1_700_000_000
    }

    const messages = toChatMessages([row])
    expect(messages[0]?.role).toBe('system')
    expect(getText(messages[0])).toBe(`↘ from charlie · ${formatShortTime(1_700_000_000)}`)
    expect(messages[0]?.asyncResult).toBe('Inbound message')
  })

  it('infers direction from message role when display_metadata omits direction', () => {
    const inbound: SessionMessage = {
      role: 'user',
      content: 'hi',
      display_kind: 'peer_message',
      display_metadata: { peer: 'dave' },
      timestamp: 1_700_000_000
    }

    const outbound: SessionMessage = {
      role: 'assistant',
      content: 'hello back',
      display_kind: 'peer_message',
      display_metadata: { peer: 'dave' }
    }

    const messages = toChatMessages([inbound, outbound])
    expect(getText(messages[0])).toContain('↘ from dave')
    expect(getText(messages[1])).toBe('↗ to dave: hello back')
  })
})
