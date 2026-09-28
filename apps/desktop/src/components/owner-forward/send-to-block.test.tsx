/**
 * D33 `:::send-to` blocks: the parse, target resolution (one / none / many / service off), that an
 * agent-authored block never sends by itself, that a trusted click opens the existing Forward sheet
 * with the exact body and target, and the per-message "Sent ✓" state after the sheet's send.
 *
 * jsdom events are never isTrusted, so the trusted path flips the one trust-check module; every
 * "untrusted" assertion runs with the real behaviour (flag off).
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import {
  createdAt,
  stubThreadEnvironment,
  stubThreadViewportSize,
  ThreadRuntime
} from '@/components/assistant-ui/test-utils'
import { Thread } from '@/components/assistant-ui/thread'
import { $forwardReceipts, $forwardSheet, setForwardGatewayRequestForTests } from '@/lib/owner-forward/client'
import {
  parseSendToDirectives,
  resolveSendToTarget,
  sendToFences,
  sendToIndexFromLanguage,
  sendToPlaceholders
} from '@/lib/owner-forward/send-to-directive'
import { resetOwnerGrantStatusForTests } from '@/lib/owner-forward/service'
import { $activeGatewayProfile } from '@/store/profile'
import { $selectedStoredSessionId, $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { ForwardSheet } from './forward-sheet'
import { type SendToOrigin, SendToOriginProvider } from './send-to-block'

const trust = vi.hoisted(() => ({ on: false }))

vi.mock('@/lib/owner-forward/trusted', () => ({
  isTrustedGesture: (event: { isTrusted?: boolean } | null | undefined) => trust.on || event?.isTrusted === true
}))

const confirm = vi.fn()
const grantStatus = vi.fn()
const gatewayRequest = vi.fn()

const READY = {
  state: 'ready',
  canSign: true,
  kid: 'ok_1',
  anchorKid: 'ok_1',
  refusal: null,
  message: 'On.',
  busy: false
}
const OFF = { state: 'off', canSign: false, kid: null, anchorKid: null, refusal: null, message: 'Off.', busy: false }

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

const BODY = 'approve: merge w6/wd-ci-hold\n\n- **keep** `ci` green, $5 cap\nsee https://example.com/x'
const directive = (target: string, body = BODY) =>
  `Please deliver this:\n\n:::send-to{session="${target}"}\n${body}\n:::\n\nThanks.`

const assistantOrigin: SendToOrigin = { session_id: 'mgr', message_id: null, role: 'assistant', messageKey: 'm1' }

function renderBlock(text: string, origin: null | SendToOrigin = assistantOrigin) {
  return render(
    <SendToOriginProvider value={origin}>
      <MarkdownTextContent isRunning={false} text={text} />
    </SendToOriginProvider>
  )
}

const sendButton = () => screen.findByRole('button', { name: /^send to /i })

beforeEach(() => {
  stubThreadEnvironment()
  stubThreadViewportSize()
  trust.on = false
  confirm.mockReset()
  grantStatus.mockReset()
  grantStatus.mockResolvedValue(READY)
  gatewayRequest.mockReset()
  ;(window as any).hermesDesktop = { ownerForward: { confirm }, ownerGrant: { status: grantStatus } }
  resetOwnerGrantStatusForTests()
  setForwardGatewayRequestForTests(gatewayRequest)
  $sessions.set([
    session('mgr', 'manager'),
    session('core_20260928_aaaa', 'cntrl-core-worker'),
    session('w1', 'worker'),
    session('w2', 'worker')
  ])
  $selectedStoredSessionId.set('mgr')
  $activeGatewayProfile.set('default')
  $forwardSheet.set(null)
  $forwardReceipts.set({})
})

afterEach(() => {
  cleanup()
  setForwardGatewayRequestForTests(null)
  $forwardSheet.set(null)
  $forwardReceipts.set({})
  delete (window as any).hermesDesktop
})

describe('parseSendToDirectives', () => {
  it('reads the session and the trimmed body exactly as written', () => {
    const [block] = parseSendToDirectives(directive('cntrl-core-worker', `\n  ${BODY}  \n`))

    expect(block).toEqual({ index: 0, session: 'cntrl-core-worker', body: BODY, closed: true })
  })

  it('finds several blocks in order, accepts single quotes and longer fences', () => {
    const text = [
      ":::send-to{session='a'}",
      'one',
      ':::',
      'prose',
      '::::send-to{session="b"}',
      'two',
      ':::',
      'still two',
      '::::'
    ].join('\n')

    expect(parseSendToDirectives(text)).toEqual([
      { index: 0, session: 'a', body: 'one', closed: true },
      { index: 1, session: 'b', body: 'two\n:::\nstill two', closed: true }
    ])
  })

  it('ignores blocks inside code fences, and a ::: inside a fenced body does not close the block', () => {
    const shown = '```md\n:::send-to{session="x"}\nnot a directive\n:::\n```'
    const fencedBody = ':::send-to{session="x"}\n```\n:::\n```\ndone\n:::'

    expect(parseSendToDirectives(shown)).toEqual([])
    expect(parseSendToDirectives(fencedBody)).toEqual([
      { index: 0, session: 'x', body: '```\n:::\n```\ndone', closed: true }
    ])
  })

  it('marks a block with no closing line, and one with no session', () => {
    expect(parseSendToDirectives(':::send-to{session="x"}\nhalf')).toEqual([
      { index: 0, session: 'x', body: 'half', closed: false }
    ])
    expect(parseSendToDirectives(':::send-to{}\nbody\n:::')[0].session).toBeNull()
  })

  it('is not fooled by prose, leaf directives or mid-line markers', () => {
    expect(parseSendToDirectives('say :::send-to{session="x"} inline\n:::')).toEqual([])
    expect(parseSendToDirectives('::send-to{session="x"}\nbody\n:::')).toEqual([])
    expect(parseSendToDirectives(':::send-to{session="x"} trailing\nbody\n:::')).toEqual([])
  })

  it('round-trips through the placeholder and fence passes to an indexed language', () => {
    const text = directive('a') + '\n\n' + directive('b')
    const fenced = sendToFences(sendToPlaceholders(text))
    const languages = [...fenced.matchAll(/^```(\S+)$/gm)].map(match => match[1])

    expect(languages.map(sendToIndexFromLanguage)).toEqual([0, 1])
    expect(fenced).not.toContain(':::send-to')
    expect(fenced).toContain('Please deliver this:')
    expect(sendToIndexFromLanguage('ts')).toBeNull()
  })
})

describe('resolveSendToTarget', () => {
  const candidates = [
    { profile: 'default', session_id: 'core_20260928_aaaa', title: 'cntrl-core-worker' },
    { profile: 'default', session_id: 'w1', title: 'worker' },
    { profile: 'default', session_id: 'w2', title: 'worker' }
  ]

  it('matches a title with or without hermes:, and an id prefix of 8+', () => {
    expect(resolveSendToTarget('cntrl-core-worker', candidates)).toMatchObject({
      status: 'one',
      target: { session_id: 'core_20260928_aaaa' }
    })
    expect(resolveSendToTarget('hermes:cntrl-core-worker', candidates)).toMatchObject({ status: 'one' })
    expect(resolveSendToTarget('CNTRL-core-worker', candidates)).toMatchObject({ status: 'one' })
    expect(resolveSendToTarget('core_2026', candidates)).toMatchObject({ status: 'one' })
  })

  it('reports none and many', () => {
    expect(resolveSendToTarget('ghost', candidates)).toEqual({ status: 'none', name: 'ghost' })
    expect(resolveSendToTarget('hermes:worker', candidates)).toEqual({ status: 'many', name: 'worker', count: 2 })
  })
})

describe('the send-to block', () => {
  it('renders the body as a quote with a live button for exactly one match', async () => {
    const { container } = renderBlock(directive('cntrl-core-worker'))

    const button = (await sendButton()) as HTMLButtonElement

    await vi.waitFor(() => expect(button.disabled).toBe(false))
    expect(button.textContent).toContain('Send to cntrl-core-worker')
    expect(container.querySelector('[data-slot="send-to-directive"] blockquote')?.textContent).toBe(BODY)
    expect(screen.getByText('Please deliver this:')).toBeTruthy()
  })

  it('disables with "No session named X" when nothing matches', async () => {
    renderBlock(directive('hermes:ghost'))

    expect(((await sendButton()) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('No session named ghost')).toBeTruthy()
  })

  it('disables with "2 sessions match X" when the name is ambiguous', async () => {
    renderBlock(directive('worker'))

    expect(((await sendButton()) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('2 sessions match worker')).toBeTruthy()
  })

  it('disables with a Settings pointer when the owner key is off', async () => {
    grantStatus.mockResolvedValue(OFF)
    renderBlock(directive('cntrl-core-worker'))

    await screen.findByText(/Owner forwarding is off.*Settings/)
    expect(((await sendButton()) as HTMLButtonElement).disabled).toBe(true)
  })

  it('disables with a Settings pointer when forwarding is unavailable (no confirm bridge)', async () => {
    ;(window as any).hermesDesktop = { ownerGrant: { status: grantStatus } }
    renderBlock(directive('cntrl-core-worker'))

    await screen.findByText(/unavailable here.*Settings/)
    expect(((await sendButton()) as HTMLButtonElement).disabled).toBe(true)
  })

  it('is not live while the message streams', async () => {
    render(
      <SendToOriginProvider value={assistantOrigin}>
        <MarkdownTextContent isRunning text={directive('cntrl-core-worker')} />
      </SendToOriginProvider>
    )

    expect(((await sendButton()) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/waiting for the message to finish/i)).toBeTruthy()
  })

  it('has no button outside a live surface (bot rooms, the owner’s own text)', async () => {
    renderBlock(directive('cntrl-core-worker'), null)

    await screen.findByText('For cntrl-core-worker')
    expect(screen.queryByRole('button', { name: /^send to /i })).toBeNull()
  })

  it('never auto-sends, and an untrusted (script) click opens nothing', async () => {
    renderBlock(directive('cntrl-core-worker'))

    const button = (await sendButton()) as HTMLButtonElement

    await vi.waitFor(() => expect(button.disabled).toBe(false))
    expect($forwardSheet.get()).toBeNull()
    fireEvent.click(button)
    button.click()
    expect($forwardSheet.get()).toBeNull()
    expect(confirm).not.toHaveBeenCalled()
    expect(gatewayRequest).not.toHaveBeenCalled()
  })

  it('a trusted click opens the Forward sheet with the exact body and target, and sends nothing yet', async () => {
    trust.on = true
    renderBlock(directive('cntrl-core-worker'))

    const button = (await sendButton()) as HTMLButtonElement

    await vi.waitFor(() => expect(button.disabled).toBe(false))
    fireEvent.click(button)

    expect($forwardSheet.get()).toMatchObject({
      text: BODY,
      gesture: 'proposal',
      origin: { session_id: 'mgr', message_id: null, role: 'assistant' },
      targets: [{ profile: 'default', session_id: 'core_20260928_aaaa', title: 'cntrl-core-worker' }],
      scope: []
    })
    expect(confirm).not.toHaveBeenCalled()
    expect(gatewayRequest).not.toHaveBeenCalled()
  })

  it('confirming in the sheet sends the exact text to the target, then the block shows Sent ✓ and keeps it', async () => {
    trust.on = true
    gatewayRequest.mockImplementation(async (method: string, params: any) =>
      method === 'secrets.mask'
        ? { text: params.text }
        : { results: [{ target_session_id: 'core_20260928_aaaa', status: 'delivered', detail: null }] }
    )
    confirm.mockResolvedValue({
      ok: true,
      decisionId: 'od',
      grantId: 'og',
      envelope: { format: 'hermes-owner-grant/v1', kid: 'k', payload: 'p', sig: 's' },
      targets: ['default:core_20260928_aaaa']
    })

    const text = directive('cntrl-core-worker')

    const view = render(
      <>
        <SendToOriginProvider value={assistantOrigin}>
          <MarkdownTextContent isRunning={false} text={text} />
        </SendToOriginProvider>
        <ForwardSheet />
      </>
    )

    const button = (await sendButton()) as HTMLButtonElement

    await vi.waitFor(() => expect(button.disabled).toBe(false))
    fireEvent.click(button)
    fireEvent.click(await screen.findByRole('button', { name: 'Send…' }))

    await vi.waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    expect(confirm.mock.calls[0][0]).toMatchObject({
      text: BODY,
      gesture: 'proposal',
      targets: [{ profile: 'default', session_id: 'core_20260928_aaaa' }],
      scope: []
    })
    // The TTL picker still applies: the sheet sends its chosen TTL.
    expect(typeof confirm.mock.calls[0][0].ttlMs).toBe('number')
    expect(gatewayRequest).toHaveBeenCalledWith('secrets.mask', expect.objectContaining({ text: BODY }))

    fireEvent.click(await screen.findByRole('button', { name: 'Done' }))
    await screen.findByText('Sent ✓')

    // Same message, remounted (virtualized away and back): still sent.
    view.unmount()
    renderBlock(text)
    await screen.findByText('Sent ✓')
    expect(screen.queryByRole('button', { name: /^send to /i })).toBeNull()

    // A different message with the same block is its own decision.
    cleanup()
    renderBlock(text, { ...assistantOrigin, messageKey: 'm2' })
    expect(await sendButton()).toBeTruthy()
  })
})

describe('in the transcript', () => {
  const assistant = (text: string) =>
    ({
      id: 'assistant-1',
      role: 'assistant',
      content: [{ type: 'text', text }],
      createdAt,
      status: { type: 'complete', reason: 'stop' },
      metadata: { custom: {} }
    }) as any

  it('an assistant message renders a live block that has not sent anything', async () => {
    render(
      <ThreadRuntime messages={[assistant(directive('cntrl-core-worker'))]}>
        <Thread />
      </ThreadRuntime>
    )

    expect(await sendButton()).toBeTruthy()
    expect($forwardSheet.get()).toBeNull()
    expect(confirm).not.toHaveBeenCalled()
    expect(gatewayRequest).not.toHaveBeenCalled()
  })

  it("the owner's own message shows the text, never a send button", async () => {
    const user = {
      id: 'user-1',
      role: 'user',
      content: [{ type: 'text', text: directive('cntrl-core-worker') }],
      createdAt,
      metadata: { custom: { rowId: 7 } }
    } as any

    render(
      <ThreadRuntime messages={[user]}>
        <Thread />
      </ThreadRuntime>
    )

    await screen.findByText(/approve: merge/)
    expect(screen.queryByRole('button', { name: /^send to /i })).toBeNull()
  })
})
