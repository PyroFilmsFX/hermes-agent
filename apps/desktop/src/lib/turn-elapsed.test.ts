import { describe, expect, it } from 'vitest'

import { formatTurnElapsed } from './turn-elapsed'

describe('formatTurnElapsed', () => {
  const base = 1_000_000_000_000 // base epoch ms

  it('formats under 1 minute as <1m', () => {
    expect(formatTurnElapsed(base, base)).toBe('<1m')
    expect(formatTurnElapsed(base, base + 30_000)).toBe('<1m')
    expect(formatTurnElapsed(base, base + 59_999)).toBe('<1m')
  })

  it('formats future / clock skew as <1m', () => {
    expect(formatTurnElapsed(base + 10_000, base)).toBe('<1m')
  })

  it('formats minutes under 1 hour as Nm', () => {
    expect(formatTurnElapsed(base, base + 60_000)).toBe('1m')
    expect(formatTurnElapsed(base, base + 120_000)).toBe('2m')
    expect(formatTurnElapsed(base, base + 15 * 60_000)).toBe('15m')
    expect(formatTurnElapsed(base, base + 59 * 60_000 + 59_000)).toBe('59m')
  })

  it('formats 1 hour or more as Hh Mm', () => {
    expect(formatTurnElapsed(base, base + 60 * 60_000)).toBe('1h 0m')
    expect(formatTurnElapsed(base, base + 65 * 60_000)).toBe('1h 5m')
    expect(formatTurnElapsed(base, base + 93 * 60_000)).toBe('1h 33m')
    expect(formatTurnElapsed(base, base + 120 * 60_000)).toBe('2h 0m')
    expect(formatTurnElapsed(base, base + (2 * 60 + 45) * 60_000)).toBe('2h 45m')
  })
})
