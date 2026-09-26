import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { WaveTrack } from './wave-track'

afterEach(cleanup)

const segments = (container: HTMLElement) =>
  Array.from(container.querySelectorAll<HTMLElement>('[data-wave]')).map(node => node.dataset.wave)

describe('WaveTrack', () => {
  it('draws one segment per wave: closed, current, then future', () => {
    const { container } = render(<WaveTrack current={3} done={2} tone="live" total={7} />)

    expect(segments(container)).toEqual(['closed', 'closed', 'current', 'future', 'future', 'future', 'future'])
    expect(screen.getByRole('img', { name: 'Wave 3 of 7, 2 closed' })).toBeTruthy()
  })

  it('says none closed on the first wave and paints the current wave in the state tone', () => {
    const { container } = render(<WaveTrack current={1} done={0} tone="attention" total={7} />)

    expect(screen.getByRole('img', { name: 'Wave 1 of 7, none closed' })).toBeTruthy()
    const current = container.querySelector<HTMLElement>('[data-wave="current"]')

    expect(current?.style.backgroundColor).toContain('--ui-yellow')
  })

  it.each([null, 0, 1])('renders nothing for a total of %s', total => {
    const { container } = render(<WaveTrack current={1} done={0} tone="live" total={total} />)

    expect(container.firstChild).toBeNull()
  })

  it('falls back to a progress bar with a count past 12 waves', () => {
    const { container } = render(<WaveTrack current={3} done={2} tone="live" total={14} />)

    expect(container.querySelector('[data-wave]')).toBeNull()
    expect(container.querySelector('[role="progressbar"]')).toBeTruthy()
    expect(container.textContent).toContain('3/14')
    expect(screen.getByRole('img', { name: 'Wave 3 of 14, 2 closed' })).toBeTruthy()
  })
})
