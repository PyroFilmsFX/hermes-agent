import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DesktopSessionBindingRecord } from '@/global'
import { setSessionRole } from '@/hermes'
import * as confirmStore from '@/store/confirm'
import { notifyError } from '@/store/notifications'
import { $projectTree, moveSessionToProject, projectRootCwd } from '@/store/projects'
import { $sessions, sessionMatchesStoredId, setSessions } from '@/store/session'
import { resetSessionBindingsForTests } from '@/store/session-binding'

import { SessionActionsMenu, SessionContextMenu } from './session-actions-menu'

afterEach(cleanup)

// Exercises the real SessionActionsMenu end-to-end (no DropdownMenu mock) so
// a broken asChild composition on the kebab trigger fails here — the menu
// must still open on click.

vi.mock('@/components/pane-shell/tree/store', () => ({
  closeAllTreeTabs: vi.fn(),
  closeOtherTreeTabs: vi.fn(),
  closeTreeTabsToRight: vi.fn(),
  treeTabCloseTargets: vi.fn(() => null)
}))
vi.mock('@/hermes', () => ({
  renameSession: vi.fn(),
  setApiRequestProfile: vi.fn(),
  setSessionRole: vi.fn(() => Promise.resolve({ ok: true })),
  setSessionUnreadRemote: vi.fn(() => Promise.resolve({ ok: true }))
}))
vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: {
        cancel: 'Cancel',
        close: 'Close',
        confirm: 'Confirm',
        delete: 'Delete',
        done: 'Done',
        loading: 'Loading…',
        save: 'Save'
      },
      errors: { genericFailure: 'Something went wrong' },
      sidebar: {
        gatewayGroups: {
          groupName: 'Group name',
          groupNameInvalid: 'Invalid group name',
          moveToGroup: 'Move to group',
          newGroup: 'New group…'
        },
        projects: {
          menuAppearance: 'Appearance',
          moveFailed: 'Could not move session',
          moveNoProjects: 'No other projects',
          movedTo: (name: string) => `Moved to ${name}`,
          moveToProject: 'Move to project',
          noColor: 'No color'
        },
        row: {
          archive: 'Archive',
          branchFrom: 'Branch from here',
          copyId: 'Copy ID',
          copyIdFailed: 'Failed to copy ID',
          deleteDesc: (title: string) => `Delete ${title}?`,
          deleteTitle: 'Delete session?',
          deleting: 'Deleting…',
          deleted: 'Session deleted',
          export: 'Export',
          hideTabBar: 'Hide tab bar',
          markRead: 'Mark as read',
          pin: 'Pin',
          rename: 'Rename',
          renameDesc: 'Leave empty to clear.',
          renameFailed: 'Rename failed',
          renameTitle: 'Rename session',
          renamed: 'Renamed',
          role: 'Role',
          roleAuto: 'Auto (from name)',
          roleFailed: 'Could not set role',
          roleNames: { manager: 'Manager', orchestrator: 'Orchestrator', stream: 'Stream', worker: 'Worker' },
          sessionActions: 'Session actions',
          unpin: 'Unpin',
          untitledPlaceholder: 'Untitled'
        }
      },
      zones: { closeAll: 'Close all', closeOthers: 'Close others', closeToRight: 'Close to the right' }
    }
  })
}))
vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))
vi.mock('@/lib/profile-color', () => ({ PROFILE_SWATCHES: [] }))
vi.mock('@/lib/session-export', () => ({ exportSession: vi.fn() }))
vi.mock('@/store/gateway', () => ({ activeGateway: vi.fn(() => null) }))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))
vi.mock('@/store/projects', () => ({
  $projectTree: atom<unknown[]>([]),
  $projectTreeLoaded: atom(true),
  refreshProjectTree: vi.fn(async () => undefined),
  moveSessionToProject: vi.fn(),
  projectIdForCwd: vi.fn(() => null),
  projectRootCwd: vi.fn(() => '')
}))
vi.mock('@/store/session', () => ({
  $activeSessionId: atom<null | string>(null),
  $connection: atom<null | { mode: string }>(null),
  $cronSessions: atom<unknown[]>([]),
  $messagingSessions: atom<unknown[]>([]),
  $selectedStoredSessionId: atom<null | string>(null),
  $sessions: atom<unknown[]>([]),
  $unreadFinishedSessionIds: atom<string[]>([]),
  markSessionRead: vi.fn(),
  sessionMatchesStoredId: vi.fn(() => false),
  sessionPinId: vi.fn((s: { id: string }) => s.id),
  setSessions: vi.fn()
}))
vi.mock('@/store/session-color', () => ({
  $sessionColorOverrides: atom<Record<string, string>>({}),
  setSessionColorOverride: vi.fn()
}))
vi.mock('@/store/session-states', () => ({
  $sessionTiles: atom<unknown[]>([]),
  closeAllOpenSessionTiles: vi.fn(),
  openSessionTile: vi.fn()
}))
vi.mock('@/store/windows', () => ({
  canOpenSessionInTerminal: () => false,
  canOpenSessionWindow: () => false,
  isBrowserWindow: () => false,
  isHudWindow: () => false,
  isSecondaryWindow: () => false,
  openSessionInNewWindow: vi.fn(),
  openSessionInTerminal: vi.fn()
}))

function renderMenu() {
  return render(
    <SessionActionsMenu sessionId="s1" title="My session">
      <button aria-label="Session actions" type="button">
        ⋮
      </button>
    </SessionActionsMenu>
  )
}

describe('SessionActionsMenu', () => {
  it('opens the dropdown on click', async () => {
    renderMenu()

    const trigger = screen.getByRole('button', { name: 'Session actions' })

    // Radix's dropdown trigger opens on pointerdown (not on the synthetic
    // 'click' fireEvent alone would dispatch), so fire the full mouse
    // sequence a real click produces.
    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)

    expect(await screen.findByRole('menu')).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: /rename/i })).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: /archive/i })).toBeTruthy()
  })

  it('opens the rename dialog focused on its input, not the row trigger', async () => {
    renderMenu()

    const trigger = screen.getByRole('button', { name: 'Session actions' })

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)

    const rename = await screen.findByRole('menuitem', { name: /rename/i })
    fireEvent.click(rename)

    // The dialog opens and its textbox takes focus. If the menu's close restored
    // focus to the row trigger instead, Space would activate the row and the
    // arrow keys would move the list rather than the caret (the reported bug).
    const dialog = await screen.findByRole('dialog')
    const input = within(dialog).getByRole('textbox')

    // eslint-disable-next-line no-restricted-globals -- asserting real focus requires the live document
    await waitFor(() => expect(document.activeElement).toBe(input))
    // eslint-disable-next-line no-restricted-globals -- asserting real focus requires the live document
    expect(document.activeElement).not.toBe(trigger)
  })

  it('confirms before deleting — cancel keeps the session, confirm deletes it', async () => {
    const onDelete = vi.fn()
    render(
      <SessionActionsMenu onDelete={onDelete} sessionId="s1" title="My session">
        <button aria-label="Session actions" type="button">
          ⋮
        </button>
      </SessionActionsMenu>
    )

    const trigger = screen.getByRole('button', { name: 'Session actions' })
    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)

    const deleteItem = await screen.findByRole('menuitem', { name: /delete/i })
    fireEvent.click(deleteItem)

    // The confirm dialog is up and names the session being deleted.
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(screen.getByText(/My session/)).toBeTruthy()

    // Cancel: nothing is deleted.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onDelete).not.toHaveBeenCalled()

    // Re-open the menu and confirm: only now does the delete call fire.
    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)
    const deleteItemAgain = await screen.findByRole('menuitem', { name: /delete/i })
    fireEvent.click(deleteItemAgain)

    expect(await screen.findByRole('dialog')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    // ConfirmDialog shows a done beat before auto-closing (600ms); awaiting it
    // also drains the async run() update inside act().
    expect(await screen.findByText('Session deleted')).toBeTruthy()
    expect(onDelete).toHaveBeenCalledTimes(1)
  })

  it('disables the delete item when no onDelete is provided', async () => {
    render(
      <SessionActionsMenu sessionId="s1" title="My session">
        <button aria-label="Session actions" type="button">
          ⋮
        </button>
      </SessionActionsMenu>
    )

    const trigger = screen.getByRole('button', { name: 'Session actions' })
    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)

    const deleteItem = await screen.findByRole('menuitem', { name: /delete/i })
    expect(deleteItem.getAttribute('aria-disabled')).toBe('true')
  })

  it('confirms with the Enter key and cancels with Escape', async () => {
    const onDelete = vi.fn()
    render(
      <SessionActionsMenu onDelete={onDelete} sessionId="s1" title="My session">
        <button aria-label="Session actions" type="button">
          ⋮
        </button>
      </SessionActionsMenu>
    )

    const trigger = screen.getByRole('button', { name: 'Session actions' })
    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('menuitem', { name: /delete/i }))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeTruthy()

    // Escape cancels: dialog closes, nothing is deleted.
    fireEvent.keyDown(window.document, { key: 'Escape' })
    expect(await screen.queryByRole('dialog')).toBeNull()
    expect(onDelete).not.toHaveBeenCalled()

    // Re-open and confirm with Enter at wherever focus actually is. Firing on
    // the dialog node would pass even when the menu leaves focus on the row
    // trigger — where Enter re-activates the row instead of confirming.
    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('menuitem', { name: /delete/i }))

    const reopened = await screen.findByRole('dialog')
    // eslint-disable-next-line no-restricted-globals -- asserting real focus requires the live document
    await waitFor(() => expect(reopened.contains(document.activeElement)).toBe(true))
    // eslint-disable-next-line no-restricted-globals -- asserting real focus requires the live document
    fireEvent.keyDown(document.activeElement!, { key: 'Enter' })

    expect(await screen.findByText('Session deleted')).toBeTruthy()
    expect(onDelete).toHaveBeenCalledTimes(1)
  })

  it('routes the same confirm guard through the context menu', async () => {
    const onDelete = vi.fn()
    render(
      <SessionContextMenu onDelete={onDelete} sessionId="s1" title="My session">
        <button aria-label="Session row" type="button">
          Row
        </button>
      </SessionContextMenu>
    )

    const row = screen.getByRole('button', { name: 'Session row' })
    fireEvent.contextMenu(row)

    fireEvent.click(await screen.findByRole('menuitem', { name: /delete/i }))
    expect(await screen.findByRole('dialog')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(await screen.findByText('Session deleted')).toBeTruthy()
    expect(onDelete).toHaveBeenCalledTimes(1)
  })
})

describe('Role submenu', () => {
  async function openRoleSubmenu() {
    render(
      <SessionContextMenu profile="tommy" sessionId="s1" title="lane-manager">
        <button aria-label="Session row" type="button">
          Row
        </button>
      </SessionContextMenu>
    )

    fireEvent.contextMenu(screen.getByRole('button', { name: 'Session row' }))
    const trigger = await screen.findByRole('menuitem', { name: /^role$/i })
    // Radix sub-triggers open on click (touch path) and ArrowRight.
    fireEvent.click(trigger)
    fireEvent.keyDown(trigger, { key: 'ArrowRight' })
  }

  afterEach(() => {
    vi.mocked(setSessionRole).mockClear()
    vi.mocked(setSessions).mockClear()
    vi.mocked(notifyError).mockClear()
  })

  it('offers Auto plus the four roles', async () => {
    await openRoleSubmenu()

    for (const name of ['Auto (from name)', 'Manager', 'Orchestrator', 'Worker', 'Stream']) {
      expect(await screen.findByRole('menuitem', { name })).toBeTruthy()
    }
  })

  it('sets an explicit role through the session PATCH and updates the row', async () => {
    await openRoleSubmenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Worker' }))

    await waitFor(() => expect(setSessionRole).toHaveBeenCalledWith('s1', 'worker', 'tommy'))
    await waitFor(() => expect(setSessions).toHaveBeenCalled())

    const update = vi.mocked(setSessions).mock.calls[0][0] as unknown as (
      prev: { id: string; session_role?: null | string }[]
    ) => { id: string; session_role?: null | string }[]

    expect(update([{ id: 's1' }, { id: 's2', session_role: 'stream' }])).toEqual([
      { id: 's1', session_role: 'worker' },
      { id: 's2', session_role: 'stream' }
    ])
  })

  it('Auto clears the explicit role', async () => {
    await openRoleSubmenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Auto (from name)' }))

    await waitFor(() => expect(setSessionRole).toHaveBeenCalledWith('s1', null, 'tommy'))
  })

  it('surfaces a failed PATCH instead of updating the row', async () => {
    vi.mocked(setSessionRole).mockRejectedValueOnce(new Error('Session not found'))
    await openRoleSubmenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Stream' }))

    await waitFor(() => expect(notifyError).toHaveBeenCalledWith(expect.any(Error), 'Could not set role'))
    expect(setSessions).not.toHaveBeenCalled()
  })
})

describe('Session binding menu entries and move-and-rebind', () => {
  let statusMock: ReturnType<typeof vi.fn>
  let setMock: ReturnType<typeof vi.fn>
  let clearMock: ReturnType<typeof vi.fn>
  let confirmSpy: ReturnType<typeof vi.spyOn>

  const TREE = [
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
    }
  ]

  const SESSION = {
    id: 's1',
    profile: 'default',
    cwd: '/r/app',
    git_repo_root: '/r/app',
    git_branch: 'main'
  }

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

  beforeEach(() => {
    resetSessionBindingsForTests()
    vi.mocked(sessionMatchesStoredId).mockImplementation((s: any, id: string) => s.id === id)
    vi.mocked(projectRootCwd).mockImplementation((node: any) => node?.path ?? '')
    vi.mocked(moveSessionToProject).mockImplementation(async () => undefined)
    $projectTree.set(TREE as any)
    $sessions.set([SESSION])

    statusMock = vi.fn(async () => record())
    setMock = vi.fn(async () => record({ state: 'bound', seq: 1 }))
    clearMock = vi.fn(async () => record({ seq: 2 }))
    ;(window as any).hermesDesktop = { sessionBinding: { set: setMock, clear: clearMock, status: statusMock } }

    confirmSpy = vi.spyOn(confirmStore, 'confirm').mockImplementation(async () => true)
  })

  afterEach(() => {
    delete (window as any).hermesDesktop
    resetSessionBindingsForTests()
    confirmSpy?.mockRestore()
    vi.mocked(sessionMatchesStoredId).mockReset()
    vi.mocked(projectRootCwd).mockReset()
    vi.mocked(moveSessionToProject).mockReset()
  })

  async function openDropdown() {
    render(
      <SessionActionsMenu profile="default" sessionId="s1" title="My session">
        <button aria-label="Session actions" type="button">
          ⋮
        </button>
      </SessionActionsMenu>
    )

    const trigger = screen.getByRole('button', { name: 'Session actions' })
    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)
    await screen.findByRole('menu')
  }

  it('entries render per state: Unbind hidden when not bound', async () => {
    statusMock.mockResolvedValue(record({ state: 'unbound' }))
    await openDropdown()

    expect(await screen.findByRole('menuitem', { name: /bind to project/i })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: /unbind/i })).toBeNull()
  })

  it('entries render per state: Unbind visible when bound', async () => {
    statusMock.mockResolvedValue(
      record({ state: 'bound', project_root: '/r/proj', repo_common_root: '/r/proj' })
    )
    await openDropdown()

    expect(await screen.findByRole('menuitem', { name: /bind to project/i })).toBeTruthy()
    expect(await screen.findByRole('menuitem', { name: /unbind/i })).toBeTruthy()
  })

  it('Unbind calls clear with {profile, hermes_session_id}', async () => {
    statusMock.mockResolvedValue(
      record({ state: 'bound', project_root: '/r/proj', repo_common_root: '/r/proj' })
    )
    await openDropdown()

    const unbindItem = await screen.findByRole('menuitem', { name: /unbind/i })
    fireEvent.click(unbindItem)

    await waitFor(() => expect(clearMock).toHaveBeenCalledTimes(1))
    expect(clearMock).toHaveBeenCalledWith({ profile: 'default', hermes_session_id: 's1' })
    expect(setMock).not.toHaveBeenCalled()
  })

  it('choosing a target calls the store set with only {profile, hermes_session_id, path}', async () => {
    statusMock.mockResolvedValue(record({ state: 'unbound' }))
    await openDropdown()

    const bindTrigger = await screen.findByRole('menuitem', { name: /bind to project/i })
    fireEvent.click(bindTrigger)
    fireEvent.keyDown(bindTrigger, { key: 'ArrowRight' })

    const targetOption = await screen.findByRole('menuitem', { name: /lane\/a/i })
    fireEvent.click(targetOption)

    await waitFor(() => expect(setMock).toHaveBeenCalledTimes(1))
    expect(setMock).toHaveBeenCalledWith({
      profile: 'default',
      hermes_session_id: 's1',
      path: '/r/proj/.worktrees/a'
    })
  })

  it('move of a bound session asks one confirm and re-binds after the move', async () => {
    statusMock.mockResolvedValue(
      record({ state: 'bound', project_root: '/r/app', repo_common_root: '/r/app' })
    )
    await openDropdown()

    const moveTrigger = await screen.findByRole('menuitem', { name: /move to project/i })
    fireEvent.click(moveTrigger)
    fireEvent.keyDown(moveTrigger, { key: 'ArrowRight' })

    const projOption = await screen.findByRole('menuitem', { name: 'Proj' })
    fireEvent.click(projOption)

    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    expect(confirmSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        title: expect.stringContaining('Proj'),
        description: expect.stringContaining('/r/proj')
      })
    )

    await waitFor(() => expect(moveSessionToProject).toHaveBeenCalledWith('s1', 'p_1', 'default'))
    await waitFor(() => expect(setMock).toHaveBeenCalledTimes(1))
    expect(setMock).toHaveBeenCalledWith({
      profile: 'default',
      hermes_session_id: 's1',
      path: '/r/proj'
    })
  })

  it('move of a bound session: cancel does neither move nor re-bind', async () => {
    confirmSpy.mockResolvedValueOnce(false)
    statusMock.mockResolvedValue(
      record({ state: 'bound', project_root: '/r/app', repo_common_root: '/r/app' })
    )
    await openDropdown()

    const moveTrigger = await screen.findByRole('menuitem', { name: /move to project/i })
    fireEvent.click(moveTrigger)
    fireEvent.keyDown(moveTrigger, { key: 'ArrowRight' })

    const projOption = await screen.findByRole('menuitem', { name: 'Proj' })
    fireEvent.click(projOption)

    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    expect(moveSessionToProject).not.toHaveBeenCalled()
    expect(setMock).not.toHaveBeenCalled()
  })

  it('move of an unbound session does not ask confirm and does not re-bind', async () => {
    statusMock.mockResolvedValue(record({ state: 'unbound' }))
    await openDropdown()

    const moveTrigger = await screen.findByRole('menuitem', { name: /move to project/i })
    fireEvent.click(moveTrigger)
    fireEvent.keyDown(moveTrigger, { key: 'ArrowRight' })

    const projOption = await screen.findByRole('menuitem', { name: 'Proj' })
    fireEvent.click(projOption)

    await waitFor(() => expect(moveSessionToProject).toHaveBeenCalledWith('s1', 'p_1', 'default'))
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(setMock).not.toHaveBeenCalled()
  })
})
