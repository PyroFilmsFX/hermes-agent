import type { MouseEvent } from 'react'

import type { ConductorRow as ConductorRowData } from '@/api/conductors'
import { Codicon } from '@/components/ui/codicon'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { githubUrlOf } from './conductor-actions'
import { SendPopover } from './send-popover'

const ICON_BUTTON =
  'grid size-5 place-items-center rounded text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background) hover:text-(--ui-text-primary)'

// A click on a control must not also open the row.
const keepFromRow = (event: MouseEvent) => event.stopPropagation()

export interface ConductorRowActionsProps {
  menuOpen: boolean
  onCopy: (text: string) => void
  onExternal: (url: string) => void
  onMenuOpenChange: (open: boolean) => void
  onOpen: () => void
  onRefresh: () => void
  onSend: (text: string) => void
  onSendOpenChange: (open: boolean) => void
  /** Can this row open its session (attributed)? */
  openable: boolean
  returnFocus: () => void
  row: ConductorRowData
  sendOpen: boolean
  sessionLabel: string
}

/**
 * The row's trailing controls: Send… and the ⋯ overflow. They float over the
 * row's right edge on hover or focus (and while either is open), so no layout
 * spends a column on them. Both buttons sit outside the tab order: the grid's
 * roving focus reaches them by key (`S` opens Send…, Shift+F10 the menu).
 */
export function ConductorRowActions({
  menuOpen,
  onCopy,
  onExternal,
  onMenuOpenChange,
  onOpen,
  onRefresh,
  onSend,
  onSendOpenChange,
  openable,
  returnFocus,
  row,
  sendOpen,
  sessionLabel
}: ConductorRowActionsProps) {
  const { t } = useI18n()
  const c = t.conductors
  const sessionId = row.orchestrator.hermes_session_id
  const ci = row.build.ci[0]
  const ciUrl = githubUrlOf(ci?.url)

  return (
    <div
      className={cn(
        'absolute top-1 right-2 flex items-center gap-0.5 rounded bg-(--ui-panel-background) p-0.5 opacity-0 group-focus-within/row:opacity-100 group-hover/row:opacity-100',
        (sendOpen || menuOpen) && 'opacity-100'
      )}
      data-slot="conductor-row-actions"
    >
      {openable && (
        <SendPopover
          onOpenChange={onSendOpenChange}
          onSubmit={onSend}
          open={sendOpen}
          returnFocus={returnFocus}
          sessionLabel={sessionLabel}
        >
          <button aria-label={c.send} className={ICON_BUTTON} onClick={keepFromRow} tabIndex={-1} type="button">
            <Codicon name="comment" size="0.75rem" />
          </button>
        </SendPopover>
      )}
      <DropdownMenu modal={false} onOpenChange={onMenuOpenChange} open={menuOpen}>
        <Tip label={c.rowActions}>
          <DropdownMenuTrigger asChild>
            <button aria-label={c.rowActions} className={ICON_BUTTON} onClick={keepFromRow} tabIndex={-1} type="button">
              <Codicon name="ellipsis" size="0.75rem" />
            </button>
          </DropdownMenuTrigger>
        </Tip>
        <DropdownMenuContent
          align="end"
          data-slot="conductor-row-menu"
          onCloseAutoFocus={event => {
            event.preventDefault()
            returnFocus()
          }}
        >
          {openable && <DropdownMenuItem onSelect={onOpen}>{c.openSession}</DropdownMenuItem>}
          {openable && <DropdownMenuItem onSelect={() => onSendOpenChange(true)}>{c.send}</DropdownMenuItem>}
          {openable && <DropdownMenuSeparator />}
          {sessionId && <DropdownMenuItem onSelect={() => onCopy(sessionId)}>{c.copySessionId}</DropdownMenuItem>}
          <DropdownMenuItem onSelect={() => onCopy(row.build.run_id)}>{c.copyRunId}</DropdownMenuItem>
          {ciUrl && (
            <DropdownMenuItem onSelect={() => onExternal(ciUrl)}>
              {ci?.pr !== null && ci?.pr !== undefined ? c.openPr(ci.pr) : c.openRun}
            </DropdownMenuItem>
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={onRefresh}>{c.refresh}</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
