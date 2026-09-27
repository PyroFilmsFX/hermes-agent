import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { useSlashCompletions } from './use-slash-completions'

const confirm = vi.fn()

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
  confirm.mockReset()
  ;(window as any).hermesDesktop = { ownerForward: { confirm } }
  $sessions.set([session('source', 'manager'), session('worker-1', 'worker-one')])
})

afterEach(() => {
  cleanup()
  delete (window as any).hermesDesktop
})

describe('/to completion', () => {
  it('inserts /to and offers session names without calling the confirm IPC', async () => {
    const gateway = { request: vi.fn(async () => ({ pairs: [], categories: [] })) }
    const { result } = renderHook(() => useSlashCompletions({ gateway: gateway as any }))
    const search = (query: string) => result.current.adapter.search?.(query) ?? []

    act(() => {
      search('to')
    })

    await waitFor(() => expect(search('to').length).toBeGreaterThan(0))
    const command = search('to')[0]
    expect(command.metadata).toMatchObject({ rawText: '/to ' })

    act(() => {
      search('to herm')
    })
    // Session suggestions arrive after the bare "/to " entry; wait for them, not for any result.
    await waitFor(() =>
      expect(search('to herm').map(item => item.metadata)).toContainEqual(
        expect.objectContaining({ rawText: '/to hermes:worker-one ' })
      )
    )
    expect(confirm).not.toHaveBeenCalled()
  })
})
