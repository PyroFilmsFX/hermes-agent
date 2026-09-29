import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import type * as ConductorsStore from '@/store/conductors'

const prList = vi.fn()
const runStatus = vi.fn()

vi.mock('@/lib/desktop-git', () => ({ desktopGit: () => ({ review: { prList, runStatus } }) }))

vi.mock('@/store/conductors', async importOriginal => {
  const actual = await importOriginal<typeof ConductorsStore>()

  return { ...actual, acquireConductorsPoller: vi.fn(() => () => {}), refreshConductors: vi.fn(async () => null) }
})

const { $conductors } = await import('@/store/conductors')
const pr = await import('@/store/pull-requests')
const { ConductorsPane } = await import('./conductors-pane')
const { makeConductorRow, makeResponse } = await import('./conductors-fixtures')

let seq = 0
const nextRoot = () => `/repo/r7-${++seq}`

const PR = {
  branch: 'feat/x',
  checks_state: 'SUCCESS',
  draft: false,
  number: 123,
  state: 'open',
  title: 'T',
  url: 'https://x/pull/123'
}

function show(row: ReturnType<typeof makeConductorRow>, visible = true) {
  $conductors.set({
    status: 'ready',
    data: makeResponse([row]),
    fetchedAt: Date.now(),
    failures: 0
  })

  return render(
    <PaneVisibleContext.Provider value={visible}>
      <ConductorsPane />
    </PaneVisibleContext.Provider>
  )
}

const rowFor = (root: string, ci: Partial<ReturnType<typeof makeConductorRow>['build']['ci'][number]>[] = []) =>
  makeConductorRow('a', {
    project: { branch: 'feat/x', root },
    build: {
      ci: ci.map(o => ({ kind: 'pr', ref: '1', branch: 'feat/x', pr: 9, state: 'success', url: '', checked_at: '', ...o }))
    }
  })

beforeEach(() => {
  vi.clearAllMocks()
  prList.mockResolvedValue({ ghReady: true, prs: [PR] })
  runStatus.mockResolvedValue({ conclusion: 'success', status: 'completed' })
  pr.$pullRequestsByBranch.set({})
  pr.$prChecksByBranch.set({})
  pr.$runStatusById.set({})
  pr.$ghRateLimit.set(null)
  pr.$ghHealth.set({ error: null, unavailable: false })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('CI/PR cell (§7)', () => {
  it('a fresh conductor ci[] entry renders as is and makes 0 gh calls', async () => {
    const { container } = show(rowFor(nextRoot(), [{ checked_at: new Date(Date.now() - 60_000).toISOString() }]))
    await act(async () => {})
    expect(container.querySelector('[data-ci-source="conductor"]')?.textContent).toContain('#9')
    expect(prList).not.toHaveBeenCalled()
    expect(runStatus).not.toHaveBeenCalled()
  })

  it('a stale conductor ci[] entry falls back to the shared cache (with checks)', async () => {
    const root = nextRoot()
    const { container } = show(rowFor(root, [{ checked_at: new Date(Date.now() - 11 * 60_000).toISOString() }]))
    await act(async () => {})
    expect(prList).toHaveBeenCalledWith(root, ['feat/x'], [], true)
    expect(container.querySelector('[data-ci-source="pr"]')?.textContent).toContain('123')
    expect(container.querySelector('[data-checks="pass"]')).toBeTruthy()
  })

  it('a hidden pane makes 0 prList / runStatus calls', async () => {
    const row = rowFor(nextRoot())
    row.build.gates = [{ ...row.build.gates[0], ref: '4242' }]
    show(row, false)
    await act(async () => {})
    expect(prList).not.toHaveBeenCalled()
    expect(runStatus).not.toHaveBeenCalled()
  })

  it('a run-id wait with no fresh observation uses runStatus when visible', async () => {
    const root = nextRoot()
    const row = makeConductorRow('a', { project: { branch: 'main', root } })
    row.build.gates = [{ ...row.build.gates[0], ref: '4242' }]
    const { container } = show(row)
    await act(async () => {})
    expect(runStatus).toHaveBeenCalledWith(root, '4242')
    expect(prList).not.toHaveBeenCalled()
    expect(container.querySelector('[data-ci-source="run"] [data-checks="pass"]')).toBeTruthy()
  })

  it('gh_unavailable shows "—" with the "gh not signed in" tooltip', async () => {
    prList.mockResolvedValue({ error: 'gh_unavailable', ghReady: false, prs: [] })
    const { container } = show(rowFor(nextRoot()))
    await act(async () => {})
    const cell = container.querySelector('[data-ci-source="unavailable"]')
    expect(cell?.textContent).toBe('—')
    await act(async () => {
      cell?.dispatchEvent(new MouseEvent('pointerover', { bubbles: true }))
      cell?.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))
    })
    expect(pr.$ghHealth.get().unavailable).toBe(true)
  })
})

describe('GitHub limit pill', () => {
  it('shows "GitHub limit: resumes HH:MM" when remaining < 200', async () => {
    const resetAt = new Date(Date.now() + 30 * 60_000).toISOString()
    prList.mockResolvedValue({ ghReady: true, prs: [PR], rate_limit: { cost: 1, remaining: 150, resetAt } })
    show(rowFor(nextRoot()))
    await act(async () => {})
    expect(screen.getByText(/^GitHub limit: resumes /)).toBeTruthy()
  })

  it('stays hidden with plenty of budget', async () => {
    prList.mockResolvedValue({ ghReady: true, prs: [PR], rate_limit: { cost: 1, remaining: 4000, resetAt: '' } })
    show(rowFor(nextRoot()))
    await act(async () => {})
    expect(screen.queryByText(/GitHub limit/)).toBeNull()
  })
})

describe('shared PR cache', () => {
  it('sidebar refresh calls never pass withChecks', async () => {
    const root = nextRoot()
    await pr.refreshPullRequests({ [root]: ['feat/x', '#7'] })
    expect(prList).toHaveBeenCalledTimes(1)
    expect(prList.mock.calls[0]).toEqual([root, ['feat/x'], [7]])
  })

  it('never asks about trunk branches', async () => {
    const row = makeConductorRow('a', { project: { branch: 'main', root: nextRoot() } })
    show(row)
    await act(async () => {})
    expect(prList).not.toHaveBeenCalled()
  })
})
