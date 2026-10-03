import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type * as ConductorsStore from '@/store/conductors'

vi.mock('@/store/conductors', async importOriginal => {
  const actual = await importOriginal<typeof ConductorsStore>()

  return { ...actual, acquireConductorsPoller: vi.fn(() => () => {}), refreshConductors: vi.fn(async () => null) }
})

const { $conductors } = await import('@/store/conductors')
const { ConductorsPane } = await import('./conductors-pane')
const { makeConductorRow, makeResponse } = await import('./conductors-fixtures')

/**
 * jsdom evaluates no CSS, so this plays the container: given the pane's width,
 * keep only the classes whose `@max-[N]` / `@min-[N]` container conditions hold
 * (Tailwind: `@max-[N]` is `width < N`, `@min-[N]` is `width >= N`) and strip
 * the variant prefixes. A later class for the same property wins, as Tailwind
 * emits narrower `@max` rules after wider ones.
 */
function activeAt(className: string, width: number): string[] {
  const active: string[] = []

  for (const token of className.split(/\s+/).filter(Boolean)) {
    let rest = token
    let ok = true

    for (;;) {
      const match = /^@(max|min)-\[(\d+)px\]:/.exec(rest)

      if (!match) {
        break
      }

      const limit = Number(match[2])
      ok &&= match[1] === 'max' ? width < limit : width >= limit
      rest = rest.slice(match[0].length)
    }

    if (ok) {
      active.push(rest)
    }
  }

  return active
}

/** The computed `display` of an element at `width`, from its classes. */
function displayAt(el: Element, width: number): string {
  let display = 'default'

  for (const cls of activeAt(el.getAttribute('class') ?? '', width)) {
    if (cls === 'hidden') {
      display = 'none'
    } else if (cls === 'flex' || cls === 'grid' || cls === 'block' || cls === 'inline') {
      display = cls
    }
  }

  return display
}

const gridAreaAt = (el: Element, width: number) =>
  activeAt(el.getAttribute('class') ?? '', width).find(cls => cls.startsWith('[grid-area:')) ?? null

const orderAt = (el: Element, width: number) => {
  const order = activeAt(el.getAttribute('class') ?? '', width).find(cls => /^order-\d+$/.test(cls))

  return order ? Number(order.slice('order-'.length)) : null
}

function renderPane() {
  $conductors.set({
    status: 'ready',
    data: makeResponse([
      makeConductorRow('a', {
        build: { ci: [{ kind: 'run', ref: '', branch: '', pr: 123, state: 'success', url: '', checked_at: '' }] }
      })
    ]),
    fetchedAt: Date.now(),
    failures: 0
  })

  const view = render(<ConductorsPane />)
  const pane = view.container.querySelector('[data-slot="conductors-pane"]')!
  const grid = view.container.querySelector('[role="grid"]')!
  const header = grid.querySelector('[role="row"]:not([data-row-key])')!
  const row = grid.querySelector('[data-row-key="a"]')!
  const cell = (col: string) => row.querySelector(`[data-col="${col}"]`)!
  const head = (col: string) => header.querySelector(`[data-col="${col}"]`)!

  return { cell, grid, head, header, pane, row }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('container, not viewport', () => {
  it('the pane root is the query container and no class uses a viewport breakpoint', () => {
    const { pane } = renderPane()
    expect(pane.className.split(/\s+/)).toContain('@container')

    const viewport = /(^|\s)(sm|md|lg|xl|2xl|max-sm|max-md|max-lg|max-xl):/

    for (const el of pane.querySelectorAll('[class]')) {
      expect(el.getAttribute('class') ?? '', el.outerHTML.slice(0, 80)).not.toMatch(viewport)
    }
  })
})

describe('wide (≥ 1100 px): ten-column table', () => {
  const W = 1280

  it('keeps the ten-track grid, the header and every column', () => {
    const { cell, grid, head, header, row } = renderPane()
    expect(displayAt(row, W)).toBe('grid')
    expect(activeAt(row.className, W).some(cls => cls.startsWith('grid-cols-[minmax(9rem'))).toBe(true)
    expect(activeAt(grid.className, W)).toContain('min-w-[68rem]')
    expect(displayAt(header, W)).toBe('grid')

    for (const col of [
      'project',
      'session',
      'now',
      'remaining',
      'estimate',
      'seats',
      'lanes',
      'ci',
      'blockers',
      'activity'
    ]) {
      expect(displayAt(cell(col), W), col).not.toBe('none')
      expect(displayAt(head(col), W), col).not.toBe('none')
      expect(gridAreaAt(cell(col), W), col).toBeNull()
    }

    // Header labels are the table's own; the merged labels stay hidden.
    const plan = [...head('remaining').querySelectorAll('span')].find(span => span.textContent === 'Plan')!
    expect(displayAt(plan, W)).toBe('none')
  })
})

describe('medium (700–1099 px): merged columns', () => {
  const W = 900

  it('switches to the eight-track grid and drops the forced min width', () => {
    const { grid, row } = renderPane()
    expect(displayAt(row, W)).toBe('grid')
    const cols = activeAt(row.className, W).filter(cls => cls.startsWith('grid-cols-'))
    expect(cols.at(-1)).toMatch(/^grid-cols-\[minmax\(5rem/)
    expect(cols.at(-1)?.split('_')).toHaveLength(8)
    expect(activeAt(grid.className, W).at(-1)).toBe('min-w-0')
  })

  it('stacks Remaining over Estimate and Lanes over Seats in one column each', () => {
    const { cell } = renderPane()
    expect(gridAreaAt(cell('remaining'), W)).toBe('[grid-area:1/4/2/5]')
    expect(gridAreaAt(cell('estimate'), W)).toBe('[grid-area:2/4/3/5]')
    expect(gridAreaAt(cell('lanes'), W)).toBe('[grid-area:1/5/2/6]')
    expect(gridAreaAt(cell('seats'), W)).toBe('[grid-area:2/5/3/6]')
    // Everything else spans both rows of its own column.
    expect(gridAreaAt(cell('ci'), W)).toBe('[grid-area:1/6/3/7]')
  })

  it('relabels the merged headers "Plan" and "Crew" and hides the absorbed ones', () => {
    const { head } = renderPane()
    expect(displayAt(head('estimate'), W)).toBe('none')
    expect(displayAt(head('lanes'), W)).toBe('none')

    const label = (col: string, text: string) =>
      [...head(col).querySelectorAll('span')].find(span => span.textContent === text)!

    expect(displayAt(label('remaining', 'Plan'), W)).toBe('inline')
    expect(displayAt(label('remaining', 'Remaining'), W)).toBe('none')
    expect(displayAt(label('seats', 'Crew'), W)).toBe('inline')
  })

  it('the crew summary drops idle seats and names the lanes', () => {
    const { cell } = renderPane()
    // Fixture: only agy has spawned lanes.
    expect(displayAt(cell('seats').querySelector('[data-seat="agy"]')!, W)).not.toBe('none')
    expect(displayAt(cell('seats').querySelector('[data-seat="codex"]')!, W)).toBe('none')
    const lanes = cell('lanes').querySelector('[data-suffix]')!
    expect(lanes.getAttribute('data-suffix')).toBe('lanes')
    expect(activeAt(lanes.className, W)).toContain('after:content-[attr(data-suffix)]')
    expect(activeAt(lanes.className, 1280)).not.toContain('after:content-[attr(data-suffix)]')
  })
})

describe('narrow (< 700 px): two-line cards', () => {
  const W = 520

  it('turns the row into a wrapping flex card and hides the header', () => {
    const { header, row } = renderPane()
    expect(displayAt(row, W)).toBe('flex')
    expect(activeAt(row.className, W)).toContain('flex-wrap')
    expect(displayAt(header, W)).toBe('none')
  })

  it('line 1 is project, session, liveness; line 2 is now, estimate, lanes, CI, blockers', () => {
    const { cell, row } = renderPane()
    const lineBreak = row.querySelector('[data-card-break]')!
    expect(displayAt(lineBreak, W)).toBe('block')
    expect(displayAt(lineBreak, 900)).toBe('none')

    const breakOrder = orderAt(lineBreak, W)!
    const line1 = ['project', 'session', 'activity'].map(col => orderAt(cell(col), W)!)
    const line2 = ['now', 'estimate', 'lanes', 'ci', 'blockers'].map(col => orderAt(cell(col), W)!)

    expect(line1).toEqual([...line1].sort((a, b) => a - b))
    expect(Math.max(...line1)).toBeLessThan(breakOrder)
    expect(line2).toEqual([...line2].sort((a, b) => a - b))
    expect(Math.min(...line2)).toBeGreaterThan(breakOrder)

    // Remaining and seats have no place on a card.
    expect(displayAt(cell('remaining'), W)).toBe('none')
    expect(displayAt(cell('seats'), W)).toBe('none')
  })

  it('separates line-2 pieces with a CSS dot, keeps the chip and drops the long-form bits', () => {
    const { cell } = renderPane()

    for (const col of ['estimate', 'lanes', 'ci', 'blockers']) {
      expect(activeAt(cell(col).className, W), col).toContain("before:content-['·']")
    }

    expect(activeAt(cell('now').className, W)).not.toContain("before:content-['·']")
    expect(cell('ci').textContent).toBe('#123 ✓')
    // "4 m ago" leaves line 1; the liveness chip stays.
    const ago = cell('activity').querySelector('.tabular-nums')!
    expect(displayAt(ago, W)).toBe('none')
    expect(displayAt(cell('activity').querySelector('[data-liveness]')!, W)).not.toBe('none')
  })
})
