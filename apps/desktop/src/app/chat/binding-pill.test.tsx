import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import type { DesktopOwnerGrantStatus, DesktopSessionBindingRecord } from '@/global'

const { confirmMock, notifyMock, selectPathsMock, projectTree } = vi.hoisted(() => ({
  confirmMock: vi.fn(async () => true),
  notifyMock: vi.fn(),
  selectPathsMock: vi.fn(async () => [] as string[]),
  projectTree: { current: null as any }
}))

vi.mock('@/store/confirm', () => ({ confirm: confirmMock }))
vi.mock('@/store/notifications', () => ({ notify: notifyMock }))
vi.mock('@/lib/desktop-fs', () => ({ isDesktopFsRemoteMode: () => false, selectDesktopPaths: selectPathsMock }))
vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  activeGateway: () => null
}))
vi.mock('@/store/projects', () => {
  projectTree.current = atom<SidebarProjectTree[]>([])

  return {
    $projectTree: projectTree.current,
    $projectTreeLoaded: atom(true),
    refreshProjectTree: vi.fn(async () => undefined)
  }
})

import { resetOwnerGrantStatusForTests, setOwnerGrantStatus } from '@/lib/owner-forward/service'
import { resetSessionBindingsForTests } from '@/store/session-binding'

import { bindingPickerSections, remoteHost, SessionBindingPill } from './binding-pill'

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.releasePointerCapture ??= () => undefined
  Element.prototype.scrollIntoView ??= () => undefined
})

function record(overrides: Partial<DesktopSessionBindingRecord> = {}): DesktopSessionBindingRecord {
  return {
    ok: true,
    state: 'unbound',
    profile: 'default',
    hermes_session_id: 's1',
    seq: 0,
    binding_nonce: null,
    bound_at: null,
    project_root: null,
    repo_common_root: null,
    repo_remote: null,
    ...overrides
  }
}

function grant(canSign: boolean): DesktopOwnerGrantStatus {
  return {
    state: canSign ? 'ready' : 'off',
    canSign,
    kid: canSign ? 'k1' : null,
    anchorKid: canSign ? 'k1' : null,
    refusal: null,
    message: '',
    busy: false
  }
}

const TREE: SidebarProjectTree[] = [
  {
    id: 'p_1',
    label: 'Proj',
    path: '/r/proj',
    repos: [
      {
        id: 'repo',
        label: 'proj',
        path: '/r/proj',
        groups: [
          { id: 'home', label: 'main', path: '/r/proj', sessions: [], branch: 'main', isHome: true },
          { id: 'lane', label: 'a', path: '/r/proj/.worktrees/a', sessions: [], branch: 'lane/a' },
          { id: 'kanban', label: 'tasks', path: '/r/proj/.worktrees/t_1', sessions: [], isKanban: true }
        ],
        sessionCount: 0
      }
    ],
    sessionCount: 0
  },
  { id: '__home__', label: 'Home', path: null, isNoProject: true, repos: [], sessionCount: 0 }
]

const GIT_SESSION = { id: 's1', profile: 'default', cwd: '/r/app', git_repo_root: '/r/app', git_branch: 'main' }
const PLAIN_SESSION = { id: 's1', profile: 'default', cwd: '/tmp/notes', git_repo_root: null }

let status: ReturnType<typeof vi.fn>
let set: ReturnType<typeof vi.fn>
let clear: ReturnType<typeof vi.fn>

beforeEach(() => {
  resetSessionBindingsForTests()
  resetOwnerGrantStatusForTests()
  setOwnerGrantStatus(grant(true))
  projectTree.current.set(TREE)
  confirmMock.mockClear()
  notifyMock.mockClear()
  status = vi.fn(async () => record())
  set = vi.fn(async () => record({ state: 'bound', project_root: '/r/proj/.worktrees/a', seq: 1 }))
  clear = vi.fn(async () => record({ seq: 2 }))
  ;(window as any).hermesDesktop = { sessionBinding: { set, clear, status } }
})

afterEach(() => {
  cleanup()
  delete (window as any).hermesDesktop
})

const pill = () => screen.getByRole('button', { name: /./ }) as HTMLButtonElement

async function openPicker() {
  const trigger = pill()
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' })

  return screen.findByRole('menu')
}

const options = () =>
  screen
    .getAllByRole('menuitem')
    .filter(el => el.dataset.slot === 'session-binding-option')
    .map(el => el.getAttribute('data-binding-path'))

describe('SessionBindingPill states', () => {
  it('unbound: muted "Bind to project" when the session has no git workspace', async () => {
    render(<SessionBindingPill session={PLAIN_SESSION} />)

    await waitFor(() => expect(status).toHaveBeenCalledTimes(1))
    expect(pill().textContent).toContain('Bind to project')
    expect(pill().dataset.state).toBe('unbound')
  })

  it('suggested: "Bind to <repo>?" from the session repo, never auto-signed', async () => {
    render(<SessionBindingPill session={GIT_SESSION} />)

    await waitFor(() => expect(pill().textContent).toContain('Bind to app?'))
    expect(pill().dataset.state).toBe('suggested')
    expect(set).not.toHaveBeenCalled()
  })

  it('bound: repo name + branch, lock glyph, remote host in the tooltip text', async () => {
    status.mockResolvedValue(
      record({
        state: 'bound',
        project_root: '/r/app',
        repo_common_root: '/r/app',
        repo_remote: 'git@github.com:org/app.git'
      })
    )
    render(<SessionBindingPill session={GIT_SESSION} />)

    await waitFor(() => expect(pill().dataset.state).toBe('bound'))
    expect(pill().textContent).toContain('app · main')
    expect(pill().querySelector('.codicon-lock')).not.toBeNull()
    expect(remoteHost('git@github.com:org/app.git')).toBe('github.com')
    expect(remoteHost('https://gitlab.example.com/org/app')).toBe('gitlab.example.com')
  })

  it('bound to a lane: the branch comes from the projects tree lane', async () => {
    status.mockResolvedValue(
      record({ state: 'bound', project_root: '/r/proj/.worktrees/a', repo_common_root: '/r/proj' })
    )
    render(<SessionBindingPill session={GIT_SESSION} />)

    await waitFor(() => expect(pill().textContent).toContain('proj · lane/a'))
  })

  it('needs re-confirm: amber "Re-confirm <repo>"', async () => {
    status.mockResolvedValue(record({ state: 'needs_reconfirm', project_root: '/r/app', repo_common_root: '/r/app' }))
    render(<SessionBindingPill session={GIT_SESSION} />)

    await waitFor(() => expect(pill().dataset.state).toBe('needs_reconfirm'))
    expect(pill().textContent).toContain('Re-confirm app · main')
    expect(pill().className).toContain('amber')
  })

  it('signing off: disabled pill that does not open the picker', async () => {
    setOwnerGrantStatus(grant(false))
    render(<SessionBindingPill session={GIT_SESSION} />)

    await waitFor(() => expect(pill().dataset.state).toBe('signing_off'))
    expect(pill().getAttribute('aria-disabled')).toBe('true')

    fireEvent.pointerDown(pill(), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(pill())
    expect(screen.queryByRole('menu')).toBeNull()
    expect(set).not.toHaveBeenCalled()
  })

  it('renders nothing without the binding IPC', () => {
    delete (window as any).hermesDesktop
    const { container } = render(<SessionBindingPill session={GIT_SESSION} />)

    expect(container.innerHTML).toBe('')
  })
})

describe('SessionBindingPill picker', () => {
  it('lists the suggested workspace first, then projects / repos / lanes (no kanban, no Home, no dupes)', async () => {
    render(<SessionBindingPill session={GIT_SESSION} />)
    await waitFor(() => expect(pill().dataset.state).toBe('suggested'))

    await openPicker()

    expect(options()).toEqual(['/r/app', '/r/proj', '/r/proj/.worktrees/a'])
    expect(screen.getByRole('menuitem', { name: /Other folder/ })).not.toBeNull()
    // Nothing bound yet: no Unbind row.
    expect(screen.queryByRole('menuitem', { name: /Unbind/ })).toBeNull()
  })

  it('a first bind asks an in-app confirm, then sets via IPC with only {profile, hermes_session_id, path}', async () => {
    render(<SessionBindingPill session={GIT_SESSION} />)
    await waitFor(() => expect(pill().dataset.state).toBe('suggested'))
    await openPicker()

    status.mockResolvedValue(
      record({ state: 'bound', project_root: '/r/proj/.worktrees/a', repo_common_root: '/r/proj' })
    )
    fireEvent.click(screen.getByRole('menuitem', { name: /lane\/a/ }))

    await waitFor(() => expect(set).toHaveBeenCalledTimes(1))
    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(set.mock.calls[0][0]).toEqual({ profile: 'default', hermes_session_id: 's1', path: '/r/proj/.worktrees/a' })
    await waitFor(() => expect(pill().dataset.state).toBe('bound'))
  })

  it('declining the in-app confirm signs nothing', async () => {
    confirmMock.mockResolvedValueOnce(false)
    render(<SessionBindingPill session={GIT_SESSION} />)
    await waitFor(() => expect(pill().dataset.state).toBe('suggested'))
    await openPicker()

    fireEvent.click(screen.getAllByRole('menuitem')[0])

    await waitFor(() => expect(confirmMock).toHaveBeenCalledTimes(1))
    await act(async () => undefined)
    expect(set).not.toHaveBeenCalled()
  })

  it('a re-bind skips the in-app confirm (main owns the native confirm for a live build)', async () => {
    status.mockResolvedValue(record({ state: 'bound', project_root: '/r/app', repo_common_root: '/r/app' }))
    render(<SessionBindingPill session={GIT_SESSION} />)
    await waitFor(() => expect(pill().dataset.state).toBe('bound'))
    await openPicker()

    fireEvent.click(screen.getByRole('menuitem', { name: /lane\/a/ }))

    await waitFor(() => expect(set).toHaveBeenCalledTimes(1))
    expect(confirmMock).not.toHaveBeenCalled()
  })

  it('Unbind calls clear', async () => {
    status.mockResolvedValue(record({ state: 'bound', project_root: '/r/app', repo_common_root: '/r/app' }))
    render(<SessionBindingPill session={GIT_SESSION} />)
    await waitFor(() => expect(pill().dataset.state).toBe('bound'))
    await openPicker()

    status.mockResolvedValue(record({ seq: 2 }))
    fireEvent.click(screen.getByRole('menuitem', { name: /Unbind/ }))

    await waitFor(() => expect(clear).toHaveBeenCalledWith({ profile: 'default', hermes_session_id: 's1' }))
    expect(set).not.toHaveBeenCalled()
    await waitFor(() => expect(pill().dataset.state).toBe('suggested'))
  })

  it('a refused bind surfaces an error toast', async () => {
    set.mockResolvedValueOnce({ ok: false, reason: 'not_git' })
    render(<SessionBindingPill session={GIT_SESSION} />)
    await waitFor(() => expect(pill().dataset.state).toBe('suggested'))
    await openPicker()

    fireEvent.click(screen.getAllByRole('menuitem')[0])

    await waitFor(() => expect(notifyMock).toHaveBeenCalledWith(expect.objectContaining({ kind: 'error' })))
  })
})

describe('bindingPickerSections', () => {
  it('drops the suggestion path from the tree rows', () => {
    const sections = bindingPickerSections(TREE, '/r/proj/')

    expect(sections.flatMap(section => section.options.map(option => option.path))).toEqual(['/r/proj/.worktrees/a'])
  })
})
