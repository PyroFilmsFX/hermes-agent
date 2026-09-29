import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $worktreeRefreshToken, refreshWorktrees, WORKTREE_REFRESH_MIN_INTERVAL_MS } from './projects'

describe('refreshWorktrees throttle', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // Every settled turn calls this; each probe is a git status per lane.
  it('runs the first call at once and collapses a burst into one trailing refresh', () => {
    const start = $worktreeRefreshToken.get()
    const now = Date.now()

    refreshWorktrees(now)
    expect($worktreeRefreshToken.get()).toBe(start + 1)

    for (let i = 1; i <= 20; i++) {
      refreshWorktrees(now + i * 100)
    }

    expect($worktreeRefreshToken.get()).toBe(start + 1)

    vi.advanceTimersByTime(WORKTREE_REFRESH_MIN_INTERVAL_MS)
    expect($worktreeRefreshToken.get()).toBe(start + 2)

    vi.advanceTimersByTime(WORKTREE_REFRESH_MIN_INTERVAL_MS * 3)
    expect($worktreeRefreshToken.get()).toBe(start + 2)
  })
})
