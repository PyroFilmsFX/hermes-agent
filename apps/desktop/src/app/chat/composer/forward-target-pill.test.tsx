/**
 * #67 / D29: the composer's "send to" control beside the model picker. It lists the `/to` targets
 * (forwardCandidates: this backend's chats minus this one), sets / clears the per-chat target, and
 * carries the forward TTL. It never sends anything itself.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $composerForwardTargets, composerForwardTargetFor } from '@/lib/owner-forward/composer-target'
import { $activeGatewayProfile } from '@/store/profile'
import { $selectedStoredSessionId, $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { ForwardTargetPill } from './forward-target-pill'

function session(id: string, title: string, lastActive = 1): SessionInfo {
  return {
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: true,
    last_active: lastActive,
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

function openMenu() {
  const trigger = screen.getByTestId('forward-target-pill')

  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' })
}

beforeEach(() => {
  ;(window as any).hermesDesktop = { ownerForward: { confirm: () => undefined } }
  $sessions.set([session('mgr_session_1', 'manager', 3), session('worker_session_1', 'worker-one', 2)])
  $selectedStoredSessionId.set('mgr_session_1')
  $activeGatewayProfile.set('default')
  $composerForwardTargets.set({})
})

afterEach(() => {
  cleanup()
  delete (window as any).hermesDesktop
})

describe('ForwardTargetPill', () => {
  it('hides without the desktop bridge or a stored chat', () => {
    delete (window as any).hermesDesktop
    const { rerender } = render(<ForwardTargetPill disabled={false} />)

    expect(screen.queryByTestId('forward-target-pill')).toBeNull()
    ;(window as any).hermesDesktop = { ownerForward: { confirm: () => undefined } }
    act(() => $selectedStoredSessionId.set(null))
    rerender(<ForwardTargetPill disabled={false} />)
    expect(screen.queryByTestId('forward-target-pill')).toBeNull()
  })

  it('picking a chat sets the target (never this chat), and the pill names it', async () => {
    render(<ForwardTargetPill disabled={false} />)
    openMenu()

    expect(screen.queryByRole('menuitem', { name: /manager/ })).toBeNull()
    fireEvent.click(await screen.findByRole('menuitem', { name: /worker-one/ }))

    expect(composerForwardTargetFor('mgr_session_1')).toMatchObject({
      session_id: 'worker_session_1',
      title: 'worker-one'
    })
    expect(screen.getByTestId('forward-target-pill').textContent).toContain('worker-one')
  })

  it('the clear button restores normal send', () => {
    $composerForwardTargets.setKey('mgr_session_1', {
      profile: 'default',
      session_id: 'worker_session_1',
      title: 'worker-one',
      ttlMs: 604_800_000
    })
    render(<ForwardTargetPill disabled={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Send here instead' }))

    expect(composerForwardTargetFor('mgr_session_1')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Send here instead' })).toBeNull()
  })
})
