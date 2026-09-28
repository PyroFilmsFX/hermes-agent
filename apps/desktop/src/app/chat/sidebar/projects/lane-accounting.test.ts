import { describe, expect, it } from 'vitest'

import type { SessionDotState } from '@/store/session-dot-state'

import {
  laneAccounting,
  type LaneAccountingEvidence,
  laneHasLiveActivity,
  partitionDoneLanes,
  sessionKeepsLaneActive
} from './lane-accounting'
import type { SidebarSessionGroup } from './workspace-groups'

const accounted: LaneAccountingEvidence = {
  clean: true,
  dotStates: {},
  mergedVia: 'merged-ancestor',
  sessions: [{ id: 's1' }]
}

const withEvidence = (over: Partial<LaneAccountingEvidence>): LaneAccountingEvidence => ({ ...accounted, ...over })

describe('laneAccounting: clause (a), the branch is accounted for in trunk', () => {
  it('(i) merged-ancestor proves it', () => {
    expect(laneAccounting(withEvidence({ mergedVia: 'merged-ancestor' }))).toEqual({
      done: true,
      reason: 'merged-ancestor'
    })
  })

  it('(ii) merged-squash proves it', () => {
    expect(laneAccounting(withEvidence({ mergedVia: 'merged-squash' }))).toEqual({
      done: true,
      reason: 'merged-squash'
    })
  })

  it('(iii) a merged PR proves it when git could not', () => {
    expect(laneAccounting(withEvidence({ mergedVia: null, prState: 'merged' }))).toEqual({
      done: true,
      reason: 'merged-pr'
    })
    expect(laneAccounting(withEvidence({ mergedVia: undefined, prState: 'MERGED' }))).toEqual({
      done: true,
      reason: 'merged-pr'
    })
  })

  it('git proof wins over the PR as the named reason', () => {
    expect(laneAccounting(withEvidence({ mergedVia: 'merged-squash', prState: 'merged' }))).toEqual({
      done: true,
      reason: 'merged-squash'
    })
  })

  it.each([
    ['probed and unproven', { mergedVia: null }],
    ['never probed (remote backend)', { mergedVia: undefined }],
    ['an unrecognised evidence value', { mergedVia: 'merged-maybe' as never }],
    ['an open PR', { mergedVia: null, prState: 'open' }],
    ['a closed, unmerged PR', { mergedVia: null, prState: 'closed' }],
    ['an unknown PR state', { mergedVia: null, prState: null }]
  ])('is NOT done on %s', (_name, over) => {
    expect(laneAccounting(withEvidence(over))).toEqual({ done: false, why: 'unaccounted' })
  })
})

describe('laneAccounting: clause (b), no session keeps the lane active', () => {
  it.each<SessionDotState>(['working', 'needs-input', 'stalled', 'background', 'unread'])(
    'a %s session keeps the lane active',
    state => {
      expect(laneAccounting(withEvidence({ dotStates: { s1: state } }))).toEqual({
        done: false,
        why: 'session-active'
      })
    }
  )

  it.each<SessionDotState>(['idle', 'draft'])('a %s session does not block done', state => {
    expect(laneAccounting(withEvidence({ dotStates: { s1: state } })).done).toBe(true)
  })

  it('the persisted row.unread keeps the lane active even with no dot state', () => {
    expect(laneAccounting(withEvidence({ sessions: [{ id: 's1', unread: true }] }))).toEqual({
      done: false,
      why: 'session-active'
    })
  })

  it('one active session among many is enough', () => {
    expect(
      laneAccounting(
        withEvidence({
          dotStates: { s3: 'working' },
          sessions: [{ id: 's1' }, { id: 's2' }, { id: 's3' }]
        })
      ).done
    ).toBe(false)
  })

  it('a lane with no sessions can be done', () => {
    expect(laneAccounting(withEvidence({ sessions: [] })).done).toBe(true)
  })
})

describe('laneAccounting: clause (c), the worktree has no uncommitted changes', () => {
  it('a dirty worktree is not done', () => {
    expect(laneAccounting(withEvidence({ clean: false }))).toEqual({ done: false, why: 'uncommitted' })
  })

  it.each([
    ['errored (null)', null],
    ['never probed (undefined)', undefined]
  ])('unknown cleanliness, %s, fails safe to not done', (_name, clean) => {
    expect(laneAccounting(withEvidence({ clean }))).toEqual({ done: false, why: 'uncommitted' })
  })

  it('a dirty worktree is not done even with a merged PR', () => {
    expect(laneAccounting(withEvidence({ clean: false, mergedVia: null, prState: 'merged' })).done).toBe(false)
  })
})

describe('session activity helpers', () => {
  it('sessionKeepsLaneActive covers the dot states and row.unread', () => {
    expect(sessionKeepsLaneActive({ id: 'a' }, { a: 'working' })).toBe(true)
    expect(sessionKeepsLaneActive({ id: 'a', unread: true }, {})).toBe(true)
    expect(sessionKeepsLaneActive({ id: 'a' }, { a: 'idle' })).toBe(false)
    expect(sessionKeepsLaneActive({ id: 'a' }, {})).toBe(false)
  })

  it('laneHasLiveActivity is true when any session is live or unread', () => {
    expect(laneHasLiveActivity([{ id: 'a' }, { id: 'b' }], { b: 'needs-input' })).toBe(true)
    expect(laneHasLiveActivity([{ id: 'a' }], { a: 'idle' })).toBe(false)
    expect(laneHasLiveActivity([], {})).toBe(false)
  })
})

describe('partitionDoneLanes', () => {
  const lane = (over: Partial<SidebarSessionGroup> & { id: string }): SidebarSessionGroup => ({
    branch: over.id,
    clean: true,
    label: over.id,
    mergedVia: 'merged-ancestor',
    path: `/repo/${over.id}`,
    sessions: [],
    ...over
  })

  it('keeps the input order, active first, and never drops a lane', () => {
    const lanes = [
      lane({ id: 'done-1' }),
      lane({ id: 'active-1', mergedVia: null }),
      lane({ id: 'done-2' }),
      lane({ id: 'active-2', clean: false })
    ]

    const { active, done } = partitionDoneLanes(lanes, {}, {}, '/repo')

    expect(active.map(l => l.id)).toEqual(['active-1', 'active-2'])
    expect(done.map(l => l.id)).toEqual(['done-1', 'done-2'])
    expect(active.length + done.length).toBe(lanes.length)
  })

  it('reads a merged PR for the lane branch from the PR map', () => {
    const lanes = [lane({ branch: 'feat/pr', id: 'pr-lane', mergedVia: null })]

    const prs = {
      '/repo\nfeat/pr': { branch: 'feat/pr', draft: false, number: 1, state: 'merged', title: 't', url: 'u' }
    }

    expect(partitionDoneLanes(lanes, {}, prs, '/repo').done.map(l => l.id)).toEqual(['pr-lane'])
    // No branch (detached): the PR join is never attempted.
    expect(partitionDoneLanes([{ ...lanes[0], branch: null }], {}, prs, '/repo').done).toEqual([])
  })

  it('a live or unread session flips a done lane back to active', () => {
    const lanes = [lane({ id: 'x', sessions: [{ id: 's' } as SidebarSessionGroup['sessions'][number]] })]

    expect(partitionDoneLanes(lanes, { s: 'idle' }, {}, '/repo').done).toHaveLength(1)
    expect(partitionDoneLanes(lanes, { s: 'working' }, {}, '/repo').active).toHaveLength(1)
    expect(partitionDoneLanes(lanes, { s: 'unread' }, {}, '/repo').active).toHaveLength(1)
  })

  it('kanban and main lanes are never done', () => {
    const lanes = [lane({ id: 'k', isKanban: true }), lane({ id: 'm', isMain: true })]

    expect(partitionDoneLanes(lanes, {}, {}, '/repo').done).toEqual([])
  })
})
