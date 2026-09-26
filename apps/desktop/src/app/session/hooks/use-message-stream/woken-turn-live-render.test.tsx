import type { GatewayEvent } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { type ChatMessage, chatMessageText } from '@/lib/chat-messages'
import { MAILBOX_ENVELOPE_FOOTER, toChatMessages } from '@/lib/chat-messages/hydration'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { SessionMessage } from '@/types/hermes'

import { renderMessageStream } from './test-harness'

// Bug #39: a session woken by a native peer message (Claude SDK lane) must render live exactly what
// hydration renders from the rows the gateway persisted for that delivery. The shapes mirror a real
// persisted delivery (tui_gateway/session_notifications.py _notif_deliver_sdk_header +
// _notif_deliver_sdk_result): header rows first, live tool events while the CLI runs the woken turn,
// then the ordered result items, each wrapped in its own background message.start/complete.

const SID = 'rt-woken'
const DELIVERY = 'deliv-peer-49a1ec6a'
const SENDER_REF = 'hermes-session:20260909_193713_ce3d96'
const RAW_LABEL = 'woken by peer message: ' + SENDER_REF
const T0 = 1_790_413_388

const envelope = (body: string) =>
  'Another Claude session sent a message:\n' +
  '<cross-session-message from="' +
  SENDER_REF +
  '" from-name="manager" via="hermes-peer-mailbox" msg-id="267">\n' +
  body +
  '\n</cross-session-message>\n\n' +
  MAILBOX_ENVELOPE_FOOTER

const PEER_TEXT = envelope('[manager] Relaying two handoff notes.')
const TEXT_A = 'Both notes change the lane briefs, so I am passing them on to the running lanes.'
const TEXT_B = 'I passed the two notes to the lanes; they take effect at each lane next step.'

const LIFECYCLE_META = { event: 'woken', source: 'peer', by: SENDER_REF, uuid: 'u-1', completed_at: T0 }
const PEER_META = { direction: 'in', peer: SENDER_REF, msg_id: 'u-1', completed_at: T0, peer_session: '' }
const RESULT_META = { completed_at: T0 + 16, source: 'sdk_background_result' }

type Tool = { id: string; name: string; args: Record<string, unknown>; result: string }

const TOOLS: Tool[] = [
  { id: 'toolu_search', name: 'ToolSearch', args: { query: 'SendMessage' }, result: '{"type":"tool_reference"}' },
  { id: 'toolu_send', name: 'SendMessage', args: { to: 'lane-1', content: 'amendment' }, result: '{"success":true}' }
]

const ev = (type: string, payload: Record<string, unknown> = {}): GatewayEvent =>
  ({ session_id: SID, type, payload }) as unknown as GatewayEvent

const toolStart = (tool: Tool) =>
  ev('tool.start', { tool_id: tool.id, name: tool.name, context: tool.name, args: tool.args })

const toolComplete = (tool: Tool) =>
  ev('tool.complete', { tool_id: tool.id, name: tool.name, args: tool.args, result: tool.result })

function headerEvents(): GatewayEvent[] {
  return [
    ev('message.start', { background: true, display_kind: 'session_lifecycle', delivery_id: DELIVERY }),
    ev('message.complete', {
      background: true,
      display_kind: 'session_lifecycle',
      status: 'complete',
      delivery_id: DELIVERY,
      text: RAW_LABEL,
      display_metadata: { ...LIFECYCLE_META, delivery_id: DELIVERY }
    }),
    ev('message.start', { background: true, display_kind: 'peer_message', delivery_id: DELIVERY }),
    ev('message.complete', {
      background: true,
      display_kind: 'peer_message',
      status: 'complete',
      delivery_id: DELIVERY,
      text: PEER_TEXT,
      display_metadata: { ...PEER_META, delivery_id: DELIVERY }
    }),
    // The assistant stream opens under the header (no user_message: the peer card was emitted).
    ev('message.start', { background: true, delivery_id: DELIVERY })
  ]
}

// While the CLI runs the woken turn the gateway's persistent tool callbacks fire (no delivery id).
const liveToolEvents = (): GatewayEvent[] => TOOLS.flatMap(tool => [toolStart(tool), toolComplete(tool)])

const textItem = (text: string): GatewayEvent[] => [
  ev('message.start', { background: true, display_kind: 'sdk_background_result', delivery_id: DELIVERY }),
  ev('message.complete', {
    background: true,
    display_kind: 'sdk_background_result',
    status: 'complete',
    delivery_id: DELIVERY,
    text,
    display_metadata: RESULT_META
  })
]

const toolItem = (tool: Tool): GatewayEvent[] => [
  ev('message.start', { background: true, delivery_id: DELIVERY }),
  toolStart(tool),
  toolComplete(tool),
  ev('message.complete', { background: true, status: 'complete', delivery_id: DELIVERY, text: '' })
]

const peerOutItem = (tool: Tool): GatewayEvent[] => [
  ev('message.start', { background: true, display_kind: 'peer_message', delivery_id: DELIVERY }),
  ev('message.complete', {
    background: true,
    display_kind: 'peer_message',
    status: 'complete',
    delivery_id: DELIVERY,
    text: 'amendment',
    display_metadata: { direction: 'out', peer: 'lane-1', msg_id: tool.id, completed_at: T0 + 16 }
  })
]

// Result items in the order _handle_unsolicited buffers them: text A, tool, tool + peer_out, text B.
const resultEvents = (): GatewayEvent[] => [
  ...textItem(TEXT_A),
  ...toolItem(TOOLS[0]),
  ...toolItem(TOOLS[1]),
  ...peerOutItem(TOOLS[1]),
  ...textItem(TEXT_B)
]

function persistedRows(): SessionMessage[] {
  const meta = { ...RESULT_META, delivery_id: DELIVERY }

  const toolRows = (tool: Tool) => [
    {
      role: 'assistant',
      content: null,
      tool_calls: [
        { id: tool.id, type: 'function', function: { name: tool.name, arguments: JSON.stringify(tool.args) } }
      ],
      display_metadata: meta,
      timestamp: T0 + 16
    },
    { role: 'tool', content: tool.result, tool_call_id: tool.id, display_metadata: meta, timestamp: T0 + 16 }
  ]

  return [
    {
      role: 'system',
      content: RAW_LABEL,
      display_kind: 'session_lifecycle',
      display_metadata: { ...LIFECYCLE_META, delivery_id: DELIVERY },
      timestamp: T0
    },
    {
      role: 'user',
      content: PEER_TEXT,
      display_kind: 'peer_message',
      display_metadata: { ...PEER_META, delivery_id: DELIVERY },
      timestamp: T0
    },
    {
      role: 'assistant',
      content: TEXT_A,
      display_kind: 'sdk_background_result',
      display_metadata: meta,
      timestamp: T0 + 16
    },
    ...toolRows(TOOLS[0]),
    ...toolRows(TOOLS[1]),
    {
      role: 'assistant',
      content: 'amendment',
      display_kind: 'peer_message',
      display_metadata: {
        direction: 'out',
        peer: 'lane-1',
        msg_id: TOOLS[1].id,
        completed_at: T0 + 16,
        delivery_id: DELIVERY
      },
      timestamp: T0 + 16
    },
    {
      role: 'assistant',
      content: TEXT_B,
      display_kind: 'sdk_background_result',
      display_metadata: meta,
      timestamp: T0 + 16
    }
  ] as unknown as SessionMessage[]
}

/** What a reader sees: role, visible text, and tool ids in order — per rendered bubble. */
function shape(messages: ChatMessage[]) {
  return messages
    .filter(m => !m.hidden)
    .map(m => ({
      role: m.role,
      // The peer card's clock suffix is formatted from its own timestamp; the name is the contract.
      text: chatMessageText(m)
        .trim()
        .replace(/ · [^·]+$/, ''),
      tools: m.parts.flatMap(part => (part.type === 'tool-call' ? [part.toolCallId] : []))
    }))
}

function runLive(events: GatewayEvent[]) {
  const states = new Map([[SID, createClientSessionState('stored-woken')]])
  const stream = renderMessageStream(SID, { states })

  act(() => {
    for (const event of events) {
      stream.handleEvent(event)
    }
  })

  return stream.state(SID).messages
}

const liveWokenTurn = () => runLive([...headerEvents(), ...liveToolEvents(), ...resultEvents()])

describe('woken turn live render equals hydration (#39)', () => {
  afterEach(() => {
    cleanup()
  })

  it('names the woken divider after the sender, not the raw session ref, live and hydrated', () => {
    for (const messages of [toChatMessages(persistedRows()), liveWokenTurn()]) {
      const divider = messages.find(m => chatMessageText(m).startsWith('woken by'))

      expect(chatMessageText(divider!)).toBe('woken by peer message: manager')
    }
  })

  it('renders each persisted assistant reply exactly once, unglued, in the hydrated order', () => {
    const live = shape(liveWokenTurn())

    expect(live).toEqual(shape(toChatMessages(persistedRows())))

    const allText = live.map(m => m.text).join('\n')

    expect(allText.split(TEXT_A)).toHaveLength(2)
    expect(allText.split(TEXT_B)).toHaveLength(2)
  })

  it('never hangs a tool card on a divider or peer card, at any point of the replay', () => {
    const events = [...headerEvents(), ...liveToolEvents(), ...resultEvents()]
    const states = new Map([[SID, createClientSessionState('stored-woken')]])
    const stream = renderMessageStream(SID, { states })

    for (const event of events) {
      act(() => stream.handleEvent(event))

      const systemTools = stream
        .state(SID)
        .messages.filter(m => m.role === 'system')
        .flatMap(m => m.parts.filter(part => part.type === 'tool-call'))

      expect(systemTools).toEqual([])
    }
  })

  it('settles replies that live deltas glued together into the persisted bubbles', () => {
    const live = runLive([
      ...headerEvents(),
      ev('message.delta', { text: TEXT_A }),
      ...liveToolEvents(),
      ev('message.delta', { text: TEXT_B }),
      ...resultEvents()
    ])

    expect(shape(live)).toEqual(shape(toChatMessages(persistedRows())))
  })

  it('a task-notification wake shows its divider once, with no echo of it as a user turn', () => {
    const label = 'woken by task notification: CI monitor'
    const meta = { event: 'woken', source: 'task-notification', by: 'CI monitor', delivery_id: DELIVERY }

    const live = runLive([
      ev('message.start', { background: true, display_kind: 'session_lifecycle', delivery_id: DELIVERY }),
      ev('message.complete', {
        background: true,
        display_kind: 'session_lifecycle',
        status: 'complete',
        delivery_id: DELIVERY,
        text: label,
        display_metadata: meta
      }),
      // _notif_deliver_sdk_header: no peer card, so the divider label rides along as user_message.
      ev('message.start', { background: true, delivery_id: DELIVERY, user_message: label, turn_author: 'peer_agent' }),
      ...textItem(TEXT_A)
    ])

    const hydrated = toChatMessages([
      { role: 'system', content: label, display_kind: 'session_lifecycle', display_metadata: meta, timestamp: T0 },
      {
        role: 'assistant',
        content: TEXT_A,
        display_kind: 'sdk_background_result',
        display_metadata: { ...RESULT_META, delivery_id: DELIVERY },
        timestamp: T0 + 1
      }
    ] as unknown as SessionMessage[])

    expect(shape(live)).toEqual(shape(hydrated))
  })

  it('leaves no tool card in the woken turn that hydration does not have', () => {
    const liveTools = shape(liveWokenTurn()).flatMap(m => m.tools)

    expect(liveTools).toEqual(shape(toChatMessages(persistedRows())).flatMap(m => m.tools))
    expect(new Set(liveTools).size).toBe(liveTools.length)
  })
})
