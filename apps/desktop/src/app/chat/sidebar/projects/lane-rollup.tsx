import type * as React from 'react'
import { useMemo } from 'react'

import type { NewSessionSplitHandler } from '@/app/chat/new-session-drag'
import { Codicon } from '@/components/ui/codicon'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { useStoreSelector, useStoresSelector } from '@/lib/use-session-slice'
import { $pullRequestsByBranch } from '@/store/pull-requests'
import { $sessionDotStateById, showsRunningArc } from '@/store/session-dot-state'

import { SidebarRowStack } from '../chrome'

import { partitionDoneLanes } from './lane-accounting'
import { useWorkspaceNodeOpen } from './model'
import { SidebarWorkspaceGroup } from './workspace-group'
import { normalizePath, type SidebarSessionGroup } from './workspace-groups'
import { WorkspaceHeader } from './workspace-header'

export interface WorktreeLaneRollupProps {
  repoRoot: string
  lanes: SidebarSessionGroup[]
  renderRows: (sessions: SessionInfo[]) => React.ReactNode
  onNewSession?: (path: null | string) => void
  onNewSessionSplit?: NewSessionSplitHandler
  onRemoveLane?: (group: SidebarSessionGroup) => void
}

// Lane ids are worktree paths; NUL can't appear in one, so it separates them in
// the primitive key the dot-state selector returns.
const ID_SEPARATOR = '\0'

/**
 * Collapsible rollup node that groups linked worktrees into one row per repo.
 *
 * Lanes split into active and done by lane-accounting.ts (owner decision D31):
 * active lanes list first, done lanes sit in a nested "Done (M)" group that is
 * collapsed by default. A done lane is never removed, and it rejoins the active
 * list the moment a session in it goes live or unread.
 */
export function WorktreeLaneRollup({
  repoRoot,
  lanes,
  renderRows,
  onNewSession,
  onNewSessionSplit,
  onRemoveLane
}: WorktreeLaneRollupProps) {
  const { t } = useI18n()
  const nodeId = `${normalizePath(repoRoot)}::lanes`
  const [open, toggleOpen] = useWorkspaceNodeOpen(nodeId, false)
  const [doneOpen, toggleDoneOpen] = useWorkspaceNodeOpen(`${nodeId}::done`, false)

  // M counts lanes containing at least one session with a running arc (live turn).
  const runningCount = useStoreSelector($sessionDotStateById, dotStates =>
    lanes.reduce((acc, lane) => {
      const isRunning = lane.sessions.some(session => showsRunningArc(dotStates[session.id] ?? 'idle'))

      return isRunning ? acc + 1 : acc
    }, 0)
  )

  // The done set as a primitive, so a dot-state edge that doesn't move a lane
  // across the line doesn't re-render the rollup.
  const doneKey = useStoresSelector([$sessionDotStateById, $pullRequestsByBranch], () =>
    partitionDoneLanes(lanes, $sessionDotStateById.get(), $pullRequestsByBranch.get(), repoRoot)
      .done.map(lane => lane.id)
      .join(ID_SEPARATOR)
  )

  const { activeLanes, doneLanes } = useMemo(() => {
    const doneIds = new Set(doneKey ? doneKey.split(ID_SEPARATOR) : [])
    const active = lanes.filter(lane => !doneIds.has(lane.id))

    return {
      // Within the active list, merged-but-not-done lanes still sink below the rest.
      activeLanes: [...active].sort((a, b) => Number(Boolean(a.merged)) - Number(Boolean(b.merged))),
      doneLanes: lanes.filter(lane => doneIds.has(lane.id))
    }
  }, [lanes, doneKey])

  if (lanes.length === 0) {
    return null
  }

  const label = t.sidebar.laneRollup(activeLanes.length, doneLanes.length, runningCount)

  const leadingIcon =
    runningCount > 0 ? (
      <span aria-hidden="true" className="size-1.5 rounded-full bg-(--ui-accent)" data-running-dot />
    ) : (
      <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="git-branch" size="0.75rem" />
    )

  const renderLane = (lane: SidebarSessionGroup) => (
    <SidebarWorkspaceGroup
      group={lane}
      key={lane.id}
      onNewSession={lane.isKanban ? undefined : onNewSession}
      onNewSessionSplit={lane.isKanban ? undefined : onNewSessionSplit}
      onRemove={lane.isMain || lane.isKanban ? undefined : () => onRemoveLane?.(lane)}
      renderRows={renderRows}
    />
  )

  return (
    <SidebarRowStack>
      <div className="relative">
        {runningCount > 0 && <span aria-hidden="true" className="arc-border arc-row" data-running-arc />}
        <WorkspaceHeader
          icon={leadingIcon}
          label={label}
          onToggle={toggleOpen}
          open={open}
        />
      </div>
      {open && (
        <SidebarRowStack className="pl-2">
          {activeLanes.map(renderLane)}
          {doneLanes.length > 0 && (
            <SidebarRowStack data-done-lanes>
              <WorkspaceHeader
                icon={<Codicon className="shrink-0 text-(--ui-text-quaternary)" name="check" size="0.75rem" />}
                label={t.sidebar.laneRollupDone(doneLanes.length)}
                onToggle={toggleDoneOpen}
                open={doneOpen}
              />
              {doneOpen && <SidebarRowStack className="pl-2">{doneLanes.map(renderLane)}</SidebarRowStack>}
            </SidebarRowStack>
          )}
        </SidebarRowStack>
      )}
    </SidebarRowStack>
  )
}
