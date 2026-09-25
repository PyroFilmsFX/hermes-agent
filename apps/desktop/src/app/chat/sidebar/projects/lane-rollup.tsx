import type * as React from 'react'

import type { NewSessionSplitHandler } from '@/app/chat/new-session-drag'
import { Codicon } from '@/components/ui/codicon'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { useStoreSelector } from '@/lib/use-session-slice'
import { $sessionDotStateById, showsRunningArc } from '@/store/session-dot-state'

import { SidebarRowStack } from '../chrome'

import { useWorkspaceNodeOpen } from './model'
import { SidebarWorkspaceGroup } from './workspace-group'
import { normalizePath, type SidebarSessionGroup } from './workspace-groups'
import { WorkspaceHeader } from './workspace-header'

export interface ConductorLaneRollupProps {
  repoRoot: string
  lanes: SidebarSessionGroup[]
  renderRows: (sessions: SessionInfo[]) => React.ReactNode
  onNewSession?: (path: null | string) => void
  onNewSessionSplit?: NewSessionSplitHandler
  onRemoveLane?: (group: SidebarSessionGroup) => void
}

/**
 * Collapsible rollup node that groups ephemeral conductor lane worktrees into
 * a single row per repo, preventing conductor lanes from flooding the sidebar.
 */
export function ConductorLaneRollup({
  repoRoot,
  lanes,
  renderRows,
  onNewSession,
  onNewSessionSplit,
  onRemoveLane
}: ConductorLaneRollupProps) {
  const { t } = useI18n()
  const nodeId = `${normalizePath(repoRoot)}::lanes`
  const [open, toggleOpen] = useWorkspaceNodeOpen(nodeId, false)

  // M counts conductor lanes containing at least one session with a running arc (live turn)
  const runningCount = useStoreSelector($sessionDotStateById, dotStates =>
    lanes.reduce((acc, lane) => {
      const isRunning = lane.sessions.some(session => showsRunningArc(dotStates[session.id] ?? 'idle'))

      return isRunning ? acc + 1 : acc
    }, 0)
  )

  if (lanes.length === 0) {
    return null
  }

  const count = lanes.length

  const label = t.sidebar.laneRollup(count, runningCount)

  const leadingIcon =
    runningCount > 0 ? (
      <span aria-hidden="true" className="size-1.5 rounded-full bg-(--ui-accent)" data-running-dot />
    ) : (
      <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="git-branch" size="0.75rem" />
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
          {lanes.map(lane => (
            <SidebarWorkspaceGroup
              group={lane}
              key={lane.id}
              onNewSession={lane.isKanban ? undefined : onNewSession}
              onNewSessionSplit={lane.isKanban ? undefined : onNewSessionSplit}
              onRemove={lane.isMain || lane.isKanban ? undefined : () => onRemoveLane?.(lane)}
              renderRows={renderRows}
            />
          ))}
        </SidebarRowStack>
      )}
    </SidebarRowStack>
  )
}
