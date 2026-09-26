import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { StatusChip } from './status-chip'

afterEach(cleanup)

const TONE_TEXT = /--ui-(purple|blue|green|yellow|orange|cyan)/

describe('StatusChip', () => {
  it.each([
    ['running', 'live', 'Running'],
    ['waiting', 'wait', 'Waiting'],
    ['succeeded', 'ok', 'Done'],
    ['stale', 'attention', 'Not responding'],
    ['timeout', 'attention', 'Timed out'],
    ['lease_expired', 'attention', 'Lease expired'],
    ['failed', 'stop', 'Failed'],
    ['blocked', 'stop', 'Blocked'],
    ['idle', 'idle', 'Idle']
  ])('%s renders tone %s with a glyph and the word %s', (status, tone, word) => {
    const { container } = render(<StatusChip status={status} />)
    const chip = container.querySelector('[data-slot="status-chip"]') as HTMLElement

    expect(chip.dataset.tone).toBe(tone)
    expect(chip.textContent).toBe(word)
    expect(chip.querySelector('[data-slot="status-chip-glyph"]')).toBeTruthy()
    expect(chip.className).toContain('whitespace-nowrap')
    expect(chip.className).toContain('shrink-0')
  })

  it('keeps label text neutral except for the stop tone, which uses red', () => {
    for (const status of ['running', 'waiting', 'succeeded', 'stale', 'timeout', 'idle']) {
      const { container, unmount } = render(<StatusChip status={status} />)
      const chip = container.querySelector('[data-slot="status-chip"]') as HTMLElement
      const label = container.querySelector('[data-slot="status-chip-label"]') as HTMLElement

      expect(chip.className).not.toMatch(TONE_TEXT)
      expect(label.className).not.toMatch(TONE_TEXT)
      expect(`${chip.className} ${label.className}`).toContain('--ui-text-secondary')
      unmount()
    }

    const { container } = render(<StatusChip status="failed" />)
    const label = container.querySelector('[data-slot="status-chip-label"]') as HTMLElement

    expect(label.className).toContain('--ui-red')
  })

  it('renders an unknown status as a non-empty attention chip', () => {
    const { container } = render(<StatusChip status="exploded" />)
    const chip = container.querySelector('[data-slot="status-chip"]') as HTMLElement

    expect(chip.dataset.tone).toBe('attention')
    expect(chip.textContent).toBe('Exploded')
  })

  it('never renders blank for an empty status', () => {
    const { container } = render(<StatusChip status="" />)

    expect(container.textContent?.trim()).not.toBe('')
  })

  it('takes a label override while the tone still follows the status', () => {
    const { container } = render(<StatusChip label="Build idle since 09:58" status="idle" />)
    const chip = container.querySelector('[data-slot="status-chip"]') as HTMLElement

    expect(chip.textContent).toBe('Build idle since 09:58')
    expect(chip.dataset.tone).toBe('idle')
  })
})
