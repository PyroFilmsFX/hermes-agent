import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { PeerMailboxListResult } from '@hermes/shared'

const mockRequest = vi.fn()

vi.mock('@/store/gateway', () => ({
  activeGateway: () => ({
    request: mockRequest
  })
}))

import { SessionMailboxPopover } from './mailbox-popover'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SessionMailboxPopover component', () => {
  it('renders trigger button and nothing if sessionId is missing', () => {
    const { container } = render(<SessionMailboxPopover sessionId="" />)
    expect(container.firstChild).toBeNull()
  })

  it('opens popover and fetches mailbox list on open', async () => {
    const emptyResult: PeerMailboxListResult = { messages: [] }
    mockRequest.mockResolvedValueOnce(emptyResult)

    render(<SessionMailboxPopover sessionId="sess-test-1" />)
    const trigger = screen.getByRole('button', { name: 'Peer Mailbox' })
    expect(trigger).toBeDefined()

    fireEvent.click(trigger)

    expect(mockRequest).toHaveBeenCalledWith('peer_mailbox.list', {
      session_id: 'sess-test-1'
    })

    await waitFor(() => {
      expect(screen.getByText('No pending peer messages')).toBeDefined()
    })
  })

  it('renders pending messages with status chip, and handles retry and cancel on queued messages', async () => {
    const messagesResult: PeerMailboxListResult = {
      messages: [
        {
          id: 101,
          direction: 'out',
          target_session_id: 'target-worker',
          target_hint: 'target-worker',
          status: 'queued',
          attempts: 2
        },
        {
          id: 102,
          direction: 'in',
          from_session_id: 'sender-coord',
          from_label: 'coordinator',
          target_session_id: 'sess-test-2',
          status: 'delivered-native',
          attempts: 0
        }
      ]
    }
    mockRequest.mockResolvedValue(messagesResult)

    render(<SessionMailboxPopover sessionId="sess-test-2" />)
    fireEvent.click(screen.getByRole('button', { name: 'Peer Mailbox' }))

    await waitFor(() => {
      expect(screen.getByText('#101')).toBeDefined()
      expect(screen.getByText('#102')).toBeDefined()
      expect(screen.getByText('↗ to target-worker')).toBeDefined()
      expect(screen.getByText('↘ from coordinator')).toBeDefined()
      expect(screen.getByText('queued (2 attempts)')).toBeDefined()
      expect(screen.getByText('delivered-native')).toBeDefined()
    })

    // Queued row has Retry and Cancel buttons
    const retryBtn = screen.getByRole('button', { name: 'Retry' })
    const cancelBtn = screen.getByRole('button', { name: 'Cancel' })
    expect(retryBtn).toBeDefined()
    expect(cancelBtn).toBeDefined()

    // Click Retry
    fireEvent.click(retryBtn)
    expect(mockRequest).toHaveBeenCalledWith('peer_mailbox.retry', {
      message_id: 101,
      session_id: 'sess-test-2'
    })

    // Click Cancel
    fireEvent.click(cancelBtn)
    expect(mockRequest).toHaveBeenCalledWith('peer_mailbox.cancel', {
      message_id: 101,
      session_id: 'sess-test-2'
    })
  })
})
