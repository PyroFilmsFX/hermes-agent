import type { HermesBranchPullRequest } from '@/global'
import type { SessionInfo } from '@/hermes'
import { branchPrKey } from '@/store/pull-requests'
import type { SessionDotState } from '@/store/session-dot-state'

import type { SidebarSessionGroup } from './workspace-groups'

/**
 * LANE ACCOUNTING: when a linked-worktree lane counts as "done" (owner decision
 * D31, superseding the S4 "hide merged lanes" idea).
 *
 * The rule, verbatim:
 *
 *   NEVER hide anything with an attached session unless it is PROVABLY
 *   accounted for. Even then it collapses into "done" and NEVER vanishes.
 *
 *   A lane is "done" iff ALL of the following hold:
 *   (a) its branch is accounted for in trunk. Any one of these proves it:
 *       (i)   the branch is an ancestor of trunk (the local trunk branch or
 *             origin/<trunk>, whichever exist)                → 'merged-ancestor'
 *       (ii)  squash or rebase merge: every commit in
 *             `git cherry <trunkref> <branch>` is prefixed '-' (patch-id
 *             equivalent) and there is at least one commit   → 'merged-squash'
 *       (iii) its PR is merged, when PR state is already available in
 *             store/pull-requests.ts                          → 'merged-pr'
 *   (b) no session in the lane is working, needs-input, stalled, background
 *       or unread ($sessionDotStateById states, plus the persisted row.unread);
 *   (c) the worktree has no uncommitted changes (`git status --porcelain` is
 *       empty).
 *   Unknown or errored evidence means NOT done. It fails safe, toward visible.
 *
 * (i) and (ii) and (c) are measured by the Electron worktree probe
 * (electron/git-worktree-ops.ts `listWorktrees`); (iii) and (b) are read from
 * the renderer stores. Everything here is pure: same inputs, same answer.
 */

/** Why a lane is done: which proof accounted its branch into trunk. */
export type LaneDoneReason = 'merged-ancestor' | 'merged-pr' | 'merged-squash'

/** Why a lane is still active. */
export type LaneActiveReason = 'session-active' | 'unaccounted' | 'uncommitted'

export type LaneAccounting = { done: false; why: LaneActiveReason } | { done: true; reason: LaneDoneReason }

type DotStates = Readonly<Record<string, SessionDotState | undefined>>
type LaneSession = Pick<SessionInfo, 'id'> & { unread?: boolean | null }

export interface LaneAccountingEvidence {
  /** Git proof from the worktree probe; null or absent = not proven. */
  mergedVia?: null | SidebarSessionGroup['mergedVia']
  /** `git status --porcelain` was empty; null or absent = unknown. */
  clean?: boolean | null
  /** The lane branch's PR state, when the PR store already has it. */
  prState?: null | string
  sessions: readonly LaneSession[]
  dotStates: DotStates
}

/** The dot states that mean "something is happening here, or waiting for you". */
const ACTIVE_DOT_STATES: ReadonlySet<SessionDotState> = new Set<SessionDotState>([
  'background',
  'needs-input',
  'stalled',
  'unread',
  'working'
])

/** Clause (b) for one session: a live turn, background work, a prompt, or unread. */
export function sessionKeepsLaneActive(session: LaneSession, dotStates: DotStates): boolean {
  const state = dotStates[session.id]

  return session.unread === true || (state !== undefined && ACTIVE_DOT_STATES.has(state))
}

/** True when any session in the lane is live or unread (clause (b) fails). */
export function laneHasLiveActivity(sessions: readonly LaneSession[], dotStates: DotStates): boolean {
  return sessions.some(session => sessionKeepsLaneActive(session, dotStates))
}

/** Apply the D31 rule to one lane's evidence. */
export function laneAccounting(evidence: LaneAccountingEvidence): LaneAccounting {
  if (laneHasLiveActivity(evidence.sessions, evidence.dotStates)) {
    return { done: false, why: 'session-active' }
  }

  // Only an explicit `true` counts: false, null (errored) and undefined (never
  // probed) all keep the lane visible.
  if (evidence.clean !== true) {
    return { done: false, why: 'uncommitted' }
  }

  if (evidence.mergedVia === 'merged-ancestor' || evidence.mergedVia === 'merged-squash') {
    return { done: true, reason: evidence.mergedVia }
  }

  if (typeof evidence.prState === 'string' && evidence.prState.toLowerCase() === 'merged') {
    return { done: true, reason: 'merged-pr' }
  }

  return { done: false, why: 'unaccounted' }
}

/** The lane branch's PR state from the PR store, or null when it isn't known. */
export function lanePrState(
  lane: Pick<SidebarSessionGroup, 'branch'>,
  pullRequests: Readonly<Record<string, HermesBranchPullRequest | undefined>>,
  repoRoot: null | string
): null | string {
  if (!repoRoot || !lane.branch) {
    return null
  }

  return pullRequests[branchPrKey(repoRoot, lane.branch)]?.state ?? null
}

/** One lane's accounting from a sidebar group plus the live stores. */
export function accountLane(
  lane: SidebarSessionGroup,
  dotStates: DotStates,
  pullRequests: Readonly<Record<string, HermesBranchPullRequest | undefined>>,
  repoRoot: null | string
): LaneAccounting {
  // The main checkout and the kanban aggregate are never lanes that retire.
  if (lane.isMain || lane.isKanban) {
    return { done: false, why: 'unaccounted' }
  }

  return laneAccounting({
    clean: lane.clean,
    dotStates,
    mergedVia: lane.mergedVia,
    prState: lanePrState(lane, pullRequests, repoRoot),
    sessions: lane.sessions
  })
}

/**
 * Split lanes into active and done, each keeping its input order. Every lane
 * lands in exactly one list: done lanes are regrouped, never dropped.
 */
export function partitionDoneLanes(
  lanes: readonly SidebarSessionGroup[],
  dotStates: DotStates,
  pullRequests: Readonly<Record<string, HermesBranchPullRequest | undefined>>,
  repoRoot: null | string
): { active: SidebarSessionGroup[]; done: SidebarSessionGroup[] } {
  const active: SidebarSessionGroup[] = []
  const done: SidebarSessionGroup[] = []

  for (const lane of lanes) {
    if (accountLane(lane, dotStates, pullRequests, repoRoot).done) {
      done.push(lane)
    } else {
      active.push(lane)
    }
  }

  return { active, done }
}
