import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { formatPeerStatusLabel, PeerStatusChip } from './system-message'

afterEach(cleanup)

describe('PeerStatusChip component', () => {
  it('renders nothing when status is empty', () => {
    const { container } = render(<PeerStatusChip status="" />)
    expect(container.firstChild).toBeNull()
  })

  it('renders delivered-native status chip with muted variant', () => {
    render(<PeerStatusChip status="delivered-native" />)
    const chip = screen.getByText('delivered-native')
    expect(chip).toBeDefined()
    expect(chip.getAttribute('data-slot')).toBe('peer-status-chip')
    expect(chip.getAttribute('data-status')).toBe('delivered-native')
    expect(chip.className).toContain('bg-muted')
  })

  it('renders delivered-live status chip with muted variant', () => {
    render(<PeerStatusChip status="delivered-live" />)
    const chip = screen.getByText('delivered-live')
    expect(chip).toBeDefined()
    expect(chip.getAttribute('data-slot')).toBe('peer-status-chip')
    expect(chip.getAttribute('data-status')).toBe('delivered-live')
    expect(chip.className).toContain('bg-muted')
  })

  it('renders resumed status chip with muted variant', () => {
    render(<PeerStatusChip status="resumed" />)
    const chip = screen.getByText('resumed')
    expect(chip).toBeDefined()
    expect(chip.getAttribute('data-slot')).toBe('peer-status-chip')
    expect(chip.getAttribute('data-status')).toBe('resumed')
    expect(chip.className).toContain('bg-muted')
  })

  it('renders queued status chip with warning variant and attempt counts', () => {
    // 0 / undefined attempts
    const { rerender } = render(<PeerStatusChip status="queued" />)
    let chip = screen.getByText('queued')
    expect(chip).toBeDefined()
    expect(chip.className).toContain('bg-amber-500/10')

    // 1 attempt
    rerender(<PeerStatusChip attempts={1} status="queued" />)
    chip = screen.getByText('queued (1 attempt)')
    expect(chip).toBeDefined()
    expect(chip.className).toContain('bg-amber-500/10')

    // Multiple attempts
    rerender(<PeerStatusChip attempts={3} status="queued" />)
    chip = screen.getByText('queued (3 attempts)')
    expect(chip).toBeDefined()
    expect(chip.className).toContain('bg-amber-500/10')
  })

  it('renders failed status chip with destructive variant', () => {
    render(<PeerStatusChip status="failed" />)
    const chip = screen.getByText('failed')
    expect(chip).toBeDefined()
    expect(chip.getAttribute('data-slot')).toBe('peer-status-chip')
    expect(chip.getAttribute('data-status')).toBe('failed')
    expect(chip.className).toContain('bg-destructive/10')
  })

  it('formatPeerStatusLabel handles singular and plural attempts correctly', () => {
    expect(formatPeerStatusLabel('queued', 1)).toBe('queued (1 attempt)')
    expect(formatPeerStatusLabel('queued', 2)).toBe('queued (2 attempts)')
    expect(formatPeerStatusLabel('queued', 0)).toBe('queued')
    expect(formatPeerStatusLabel('delivered-live')).toBe('delivered-live')
    expect(formatPeerStatusLabel('failed')).toBe('failed')
  })
})
