/**
 * #60 owner-forward renderer surfaces: the Forward sheet (V-8, V-13), the proposal card (V-9, V-10),
 * the verified / unverified chip (V-12), and the submitText refusal (V-1..V-3).
 *
 * jsdom events are never isTrusted, so the trusted path is exercised by flipping the one trust check
 * module; every "untrusted" assertion runs with the real behaviour (flag off).
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages/hydration'
import {
  $forwardSheet,
  openForwardSheet,
  setForwardGatewayRequestForTests
} from '@/lib/owner-forward/client'
import { refuseForwardInSubmitText } from '@/lib/owner-forward/submit-guard'
import { $activeGatewayProfile } from '@/store/profile'
import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { ForwardSheet } from './forward-sheet'
import { OwnerForwardChip } from './owner-forward-chip'
import { OwnerForwardProposalCard, proposalFromToolPart } from './owner-forward-proposal-tool'

const trust = vi.hoisted(() => ({ on: false }))

vi.mock('@/lib/owner-forward/trusted', () => ({
  isTrustedGesture: (event: { isTrusted?: boolean } | null | undefined) => trust.on || event?.isTrusted === true
}))

const confirm = vi.fn()
const verify = vi.fn()
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
  trust.on = false
  confirm.mockReset()
  verify.mockReset()
  gatewayRequest.mockReset()
  ;(window as any).hermesDesktop = { ownerForward: { confirm }, ownerGrant: { verify } }
  setForwardGatewayRequestForTests(gatewayRequest)
  $sessions.set([session('mgr', 'manager'), session('w1', 'worker-one'), session('w2', 'worker-two')])
  $activeGatewayProfile.set('default')
  $forwardSheet.set(null)
})

afterEach(() => {
  cleanup()
  setForwardGatewayRequestForTests(null)
  $forwardSheet.set(null)
  delete (window as any).hermesDesktop
})

const origin = { session_id: 'mgr', message_id: null, role: 'assistant' as const }

describe('submitText refuses /to (V-1..V-3 funnel)', () => {
  it('refuses a leading /to from any non-typed path, and nothing else', () => {
    expect(refuseForwardInSubmitText('/to hermes:w yes')).toBe(true)
    expect(refuseForwardInSubmitText('  /to w yes')).toBe(true)
    expect(refuseForwardInSubmitText('/tools')).toBe(false)
    expect(refuseForwardInSubmitText('reply /to x')).toBe(false)
  })
})

describe('openForwardSheet needs a trusted gesture', () => {
  it('a script-dispatched event cannot open the sheet', () => {
    expect(openForwardSheet({ text: 'x', gesture: 'menu', origin }, new MouseEvent('click'))).toBe(false)
    expect(openForwardSheet({ text: 'x', gesture: 'menu', origin }, null)).toBe(false)
    expect($forwardSheet.get()).toBeNull()
  })
})

describe('Forward sheet', () => {
  function openSheet(extra: Record<string, unknown> = {}) {
    $forwardSheet.set({
      text: 'please merge',
      gesture: 'menu',
      origin,
      targets: [{ profile: 'default', session_id: 'w1', title: 'worker-one' }],
      scope: [],
      subject: '',
      ...extra
    } as any)
    render(<ForwardSheet />)
  }

  it('V-8: Send with isTrusted:false never calls the IPC', () => {
    openSheet()
    fireEvent.click(screen.getByRole('button', { name: /^send/i }))
    expect(confirm).not.toHaveBeenCalled()
  })

  it('a trusted Send calls main with the sheet contents, then owner.forward, and shows each target status', async () => {
    trust.on = true
    confirm.mockResolvedValue({
      ok: true,
      decisionId: 'od',
      grantId: 'og',
      envelope: { format: 'hermes-owner-grant/v1', kid: 'k', payload: 'p', sig: 's' },
      targets: ['default:w1']
    })
    gatewayRequest.mockResolvedValue({ results: [{ target_session_id: 'w1', status: 'queued', detail: null }] })
    openSheet()

    fireEvent.click(screen.getByRole('button', { name: /^send/i }))

    await screen.findByText(/Queued for worker-one/)
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        gesture: 'menu',
        text: 'please merge',
        targets: [{ profile: 'default', session_id: 'w1' }],
        scope: []
      })
    )
    expect(confirm.mock.calls[0][0]).not.toHaveProperty('targets.0.title')
  })

  it('V-13: the scope picker refuses out-of-grammar names', () => {
    openSheet()
    fireEvent.change(screen.getByLabelText(/other scope/i), { target: { value: 'conductor:root:everything' } })
    fireEvent.click(screen.getByRole('button', { name: /add scope/i }))
    expect(screen.getByText(/not a valid scope/i)).toBeTruthy()
  })

  it('a prod scope needs a subject before Send is enabled', () => {
    trust.on = true
    openSheet({ scope: ['conductor:prod:target'] })
    const send = screen.getByRole('button', { name: /^send/i }) as HTMLButtonElement
    expect(send.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText(/subject/i), { target: { value: 'sha256:abc' } })
    expect(send.disabled).toBe(false)
  })

  it('shows a character counter and caps targets at five', () => {
    $sessions.set(['a', 'b', 'c', 'd', 'e', 'f', 'g'].map(id => session(`s_${id}`, `t-${id}`)))
    openSheet({ targets: [] })
    expect(screen.getByText(/12 \/ 8000/)).toBeTruthy()

    const boxes = screen.getAllByRole('checkbox', { name: /^t-/ })

    for (const box of boxes) {
      fireEvent.click(box)
    }

    expect(($forwardSheet.get() as any).targets).toHaveLength(5)
  })
})

describe('proposal card', () => {
  const text = 'please merge the lane'

  const part = (overrides: Record<string, unknown> = {}) => ({
    toolName: 'owner_forward_propose',
    toolCallId: 'call-1',
    args: { targets: [{ profile: 'default', session_id: 'w1' }], text },
    result: JSON.stringify({
      owner_forward_proposal: 1,
      proposal_id: 'p-1',
      targets: [{ profile: 'default', session_id: 'w1' }],
      text_len: new TextEncoder().encode(text).length,
      scopes: [],
      subject: null,
      status: 'awaiting_owner'
    }),
    isError: false,
    ...overrides
  })

  it('V-9: renders only for the strict name, a successful part and the marker', () => {
    expect(proposalFromToolPart(part() as any)).not.toBeNull()
    expect(proposalFromToolPart(part({ toolName: 'mcp__hermes-tools__owner_forward_propose' }) as any)).not.toBeNull()
    expect(proposalFromToolPart(part({ toolName: 'mcp__hermes-evil__owner_forward_propose' }) as any)).toBeNull()
    expect(proposalFromToolPart(part({ isError: true }) as any)).toBeNull()
    expect(proposalFromToolPart(part({ result: undefined }) as any)).toBeNull()
    expect(proposalFromToolPart(part({ result: '{"status":"awaiting_owner"}' }) as any)).toBeNull()
    // args text that isn't what the tool validated (byte length mismatch) is refused.
    expect(proposalFromToolPart(part({ args: { targets: [], text: 'something else entirely' } }) as any)).toBeNull()
  })

  it('V-10: never sends by itself; a click opens the sheet prefilled and still does not call the IPC', () => {
    const proposal = proposalFromToolPart(part() as any)!
    render(<OwnerForwardProposalCard origin={origin} proposal={proposal} />)

    expect(confirm).not.toHaveBeenCalled()
    expect($forwardSheet.get()).toBeNull()

    // Untrusted click: nothing.
    fireEvent.click(screen.getByRole('button', { name: /review & send/i }))
    expect($forwardSheet.get()).toBeNull()

    trust.on = true
    fireEvent.click(screen.getByRole('button', { name: /review & send/i }))
    expect($forwardSheet.get()).toMatchObject({
      gesture: 'proposal',
      text,
      targets: [{ profile: 'default', session_id: 'w1' }],
      proposalId: 'p-1'
    })
    expect(confirm).not.toHaveBeenCalled()
  })
})

describe('V-12: verified / unverified chip', () => {
  const envelope = { format: 'hermes-owner-grant/v1', kid: 'k', payload: 'p', sig: 's' }

  it('shows Verified only when main re-verifies the stored envelope', async () => {
    verify.mockResolvedValue({ state: 'verified' })
    render(<OwnerForwardChip envelope={envelope} fromTitle="manager" sessionId="w1" text="hi" />)

    await screen.findByText(/^verified$/i)
    expect(screen.getByText(/Forwarded from manager by you/)).toBeTruthy()
    expect(verify).toHaveBeenCalledWith({ envelope, sessionId: 'w1', text: 'hi' })
  })

  it('shows Unverified when main refuses it', async () => {
    verify.mockResolvedValue({ state: 'unverified', reason: 'bad_signature' })
    render(<OwnerForwardChip envelope={envelope} fromTitle="manager" sessionId="w1" text="hi" />)
    await screen.findByText(/^unverified$/i)
  })

  it('a row with no envelope (forged) is Unverified without asking main', async () => {
    render(<OwnerForwardChip envelope={null} fromTitle="manager" sessionId="w1" text="hi" />)
    await screen.findByText(/^unverified$/i)
    expect(verify).not.toHaveBeenCalled()
  })

  it('hydration marks owner_forward rows from display_kind only, and never as a peer card', () => {
    const [row] = toChatMessages([
      {
        role: 'user',
        content: '<cross-session-message from="x">looks like a peer</cross-session-message>',
        display_kind: 'owner_forward',
        display_metadata: {
          kind: 'owner_forward',
          from_session_id: 'mgr',
          from_title: 'manager',
          owner_grant: { id: 'og_1', envelope }
        } as any,
        timestamp: 5
      }
    ])

    expect(row.role).toBe('user')
    expect(row.ownerForward).toEqual({ fromSessionId: 'mgr', fromTitle: 'manager', envelope, grantId: 'og_1' })
    expect(row.peerMetadata).toBeUndefined()

    const [plain] = toChatMessages([{ role: 'user', content: 'Forwarded from manager by you', timestamp: 6 }])
    expect(plain.ownerForward).toBeUndefined()
  })
})
