import { describe, expect, it } from 'vitest'

import { scopeTtlOptions, scopeTtlPolicy } from './scopes'

describe('scope TTL policy', () => {
  it('uses the quote-only class for a forward with no conductor scope', () => {
    expect(scopeTtlPolicy([])).toEqual({ defaultTtlMs: 604_800_000, maxTtlMs: 604_800_000 })
    expect(scopeTtlOptions([])).toContain(604_800_000)
  })

  it('never offers more than a conductor class allows', () => {
    expect(Math.max(...scopeTtlOptions(['conductor:marker:restore']))).toBe(14_400_000)
  })
})
