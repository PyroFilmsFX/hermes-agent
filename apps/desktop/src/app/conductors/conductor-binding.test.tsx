import { render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopSessionBindingRecord } from '@/global'
import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import type * as ConductorsStore from '@/store/conductors'
import type * as BindingStore from '@/store/session-binding'
import type { ProjectInfo } from '@/types/hermes'

vi.mock('@/store/conductors', async importOriginal => {
  const actual = await importOriginal<typeof ConductorsStore>()

  return { ...actual, acquireConductorsPoller: vi.fn(() => () => undefined), refreshConductors: vi.fn(async () => null) }
})

vi.mock('@/store/session-binding', async importOriginal => {
  const actual = await importOriginal<typeof BindingStore>()

  return { ...actual, ensureSessionBinding: vi.fn() }
})

const { $conductors } = await import('@/store/conductors')
const { $projects } = await import('@/store/projects')
const { $sessionBindings, ensureSessionBinding, sessionBindingKey } = await import('@/store/session-binding')
const { rowBinding } = await import('./conductor-binding')
const { ConductorRow } = await import('./conductor-row')
const { ConductorsPane } = await import('./conductors-pane')
const { makeConductorRow, makeResponse } = await import('./conductors-fixtures')

const ensure = vi.mocked(ensureSessionBinding)

const ROOT = '/work/acme-app'

const project = (name: string, primary: string): ProjectInfo => ({
  id: name,
  slug: name,
  name,
  description: null,
  icon: null,
  color: null,
  board_slug: null,
  primary_path: primary,
  archived: false,
  created_at: 0,
  folders: []
})

const record = (over: Partial<DesktopSessionBindingRecord> = {}): DesktopSessionBindingRecord => ({
  ok: true,
  state: 'bound',
  profile: 'default',
  hermes_session_id: 'hs-a',
  seq: 1,
  binding_nonce: 'n',
  bound_at: 1,
  project_root: ROOT,
  repo_common_root: null,
  repo_remote: null,
  verified: true,
  ...over
})

const ready = (rec: DesktopSessionBindingRecord) => ({
  [sessionBindingKey('default', 'hs-a')]: { status: 'ready' as const, record: rec, at: Date.now() }
})

const row = makeConductorRow('a', { orchestrator: { hermes_session_id: 'hs-a', profile: 'default' } })

const renderRow = (binding: ReturnType<typeof rowBinding>) =>
  render(
    <div role="grid">
      <ConductorRow abandoned={false} activeProfile="default" binding={binding} row={row} />
    </div>
  )

beforeEach(() => {
  vi.clearAllMocks()
  $sessionBindings.set({})
  $projects.set([])
  $conductors.set({ status: 'idle', data: null, fetchedAt: null, failures: 0 })
})

afterEach(() => {
  $sessionBindings.set({})
  $projects.set([])
})

describe('rowBinding', () => {
  it('a verified bound record takes the matching project name', () => {
    const projects = [project('Acme', ROOT)]
    expect(rowBinding(row, ready(record()), projects)).toEqual({ bound: true, label: 'Acme' })
  })

  it('falls back to the path leaf when no project matches', () => {
    expect(rowBinding(row, ready(record()), [project('Other', '/elsewhere')])).toEqual({
      bound: true,
      label: 'acme-app'
    })
  })

  it.each([
    ['needs_reconfirm', record({ state: 'needs_reconfirm' })],
    ['unbound', record({ state: 'unbound', project_root: null })],
    ['verified:false', record({ verified: false })]
  ])('%s is not bound', (_name, rec) => {
    expect(rowBinding(row, ready(rec), [])).toEqual({ bound: false, label: null })
  })

  it('loading, error and unknown sessions are not bound', () => {
    const key = sessionBindingKey('default', 'hs-a')

    const loading = { [key]: { status: 'loading' as const, record: record(), at: 0 } }
    const error = { [key]: { status: 'error' as const, record: record(), reason: 'x', at: 0 } }

    expect(rowBinding(row, loading, [])).toEqual({ bound: false, label: null })
    expect(rowBinding(row, error, [])).toEqual({ bound: false, label: null })
    expect(rowBinding(row, {}, [])).toEqual({ bound: false, label: null })
  })
})

describe('row rendering', () => {
  it('bound: shows the project label and the chip instead of the workspace name', () => {
    const { container } = renderRow({ bound: true, label: 'Acme' })
    expect(container.textContent).toContain('Acme')
    expect(container.textContent).not.toContain('project-a')
    expect(container.querySelector('[data-slot="conductor-bound-chip"]')?.textContent).toBe('bound')
  })

  it('not bound: keeps the workspace name and no chip', () => {
    const { container } = renderRow({ bound: false, label: null })
    expect(container.textContent).toContain('project-a')
    expect(container.querySelector('[data-slot="conductor-bound-chip"]')).toBeNull()
  })
})

describe('pane binding reads', () => {
  const rows = [
    makeConductorRow('a', { orchestrator: { hermes_session_id: 'hs-a', profile: 'default' } }),
    makeConductorRow('b', { orchestrator: { hermes_session_id: 'hs-a', profile: 'default' } }),
    makeConductorRow('c', { orchestrator: { hermes_session_id: 'hs-c', profile: 'default' } }),
    makeConductorRow('d', { orchestrator: { hermes_session_id: null } })
  ]

  const view = (visible: boolean) => (
    <PaneVisibleContext.Provider value={visible}>
      <ConductorsPane />
    </PaneVisibleContext.Provider>
  )

  it('a hidden pane never asks', () => {
    $conductors.set({ status: 'ready', data: makeResponse(rows), fetchedAt: Date.now(), failures: 0 })
    render(view(false))
    expect(ensure).toHaveBeenCalledTimes(0)
  })

  it('a visible pane asks once per distinct (profile, session)', () => {
    $conductors.set({ status: 'ready', data: makeResponse(rows), fetchedAt: Date.now(), failures: 0 })
    render(view(true))
    expect(ensure).toHaveBeenCalledTimes(2)
    expect(ensure).toHaveBeenCalledWith('default', 'hs-a')
    expect(ensure).toHaveBeenCalledWith('default', 'hs-c')
  })
})
