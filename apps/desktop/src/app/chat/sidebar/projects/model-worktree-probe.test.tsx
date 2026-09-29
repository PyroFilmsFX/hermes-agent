import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const worktreeList = vi.fn(async (_repoPath: string) => [])

vi.mock('@/lib/desktop-git', () => ({
  desktopGit: () => ({ worktreeList })
}))

import { useRepoWorktreeMap } from './model'

afterEach(() => {
  worktreeList.mockClear()
})

describe('useRepoWorktreeMap', () => {
  // Each probe is a full per-lane git scan in the main process. Callers rebuild
  // the path array on unrelated renders; that alone must not start a new scan.
  it('does not re-probe when the caller passes a new array with the same repos', async () => {
    const { rerender } = renderHook(({ paths }) => useRepoWorktreeMap(paths, true), {
      initialProps: { paths: ['/repos/a', '/repos/b'] }
    })

    await waitFor(() => expect(worktreeList).toHaveBeenCalledTimes(2))

    rerender({ paths: ['/repos/b', '/repos/a'] })
    rerender({ paths: ['/repos/a', '/repos/b'] })

    await new Promise(resolve => setTimeout(resolve, 20))
    expect(worktreeList).toHaveBeenCalledTimes(2)
  })

  it('probes again when the set of repos really changes', async () => {
    const { rerender } = renderHook(({ paths }) => useRepoWorktreeMap(paths, true), {
      initialProps: { paths: ['/repos/a'] }
    })

    await waitFor(() => expect(worktreeList).toHaveBeenCalledTimes(1))

    rerender({ paths: ['/repos/a', '/repos/c'] })

    await waitFor(() => expect(worktreeList).toHaveBeenCalledTimes(3))
    expect(worktreeList).toHaveBeenLastCalledWith('/repos/c')
  })
})
