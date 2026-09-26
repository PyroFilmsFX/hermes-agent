import { describe, expect, it } from 'vitest'

import { planTitle } from './plan-title'

describe('planTitle', () => {
  it.each([
    ['OVERNIGHT-HERMES-WORKER-2026-09-26', 'Overnight Hermes Worker'],
    ['HE-G1-STATUS-STRIP-DESIGN-2026-09-26', 'HE G1 Status Strip Design'],
    ['HE-CONDUCTOR-UI-REDESIGN-2026-09-26', 'HE Conductor UI Redesign'],
    ['tb-plan', 'Build plan'],
    ['lane_g9_design', 'Lane g9 Design']
  ])('%s reads as %s', (stem, title) => {
    expect(planTitle(stem)).toBe(title)
  })
})
