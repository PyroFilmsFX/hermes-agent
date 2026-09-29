import { useStore } from '@nanostores/react'
import type { ReactNode } from 'react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { notifyError } from '@/store/notifications'
import { $sidebarSessionRankIds } from '@/store/sidebar-sort'

import { SidebarGroupRow, SidebarRowLead, SidebarRowLeadGlyph, SidebarRowLink, SidebarRowStack } from './chrome'
import {
  $cntrlGroupCollapsed,
  CNTRL_GROUP_UNGROUPED,
  type CntrlGroup,
  refreshCntrlGroups,
  reorderCntrlGroups,
  toggleCntrlGroupCollapsed,
  ungroupAllCntrlGroup,
  updateCntrlGroup
} from './cntrl-groups'
import { rankSessions } from './order'
import { SIDEBAR_GROUP_PAGE } from './projects/model'
import { WorkspaceShowMoreButton } from './projects/workspace-header'
import { SIDEBAR_LEAD_ICON_SIZE } from './row-geometry'

const UNGROUPED_ID = CNTRL_GROUP_UNGROUPED

function GroupHeaderMenu({ group, groups, label }: { group: CntrlGroup; groups: CntrlGroup[]; label: string }) {
  const { t } = useI18n()
  const c = t.sidebar.gatewayGroups
  const [renaming, setRenaming] = useState(false)
  const [name, setName] = useState(group.name)
  const [busy, setBusy] = useState(false)
  // Pinned and unpinned groups order independently (pinned always lead), so a
  // move only ever reorders within the group's own band.
  const peers = groups.filter(item => item.pinned === group.pinned)
  const peerIndex = peers.findIndex(item => item.name === group.name)

  const move = (direction: -1 | 1) => {
    const target = peerIndex + direction

    if (peerIndex < 0 || target < 0 || target >= peers.length) {
      return
    }

    // Renumber the whole band: fresh groups all carry order 0, so swapping two
    // orders would be a no-op and the menu item would silently do nothing.
    const names = peers.map(item => item.name)
    names.splice(peerIndex, 1)
    names.splice(target, 0, group.name)
    void reorderCntrlGroups(names, peers).catch(error => notifyError(error, c.actions))
  }

  const rename = async () => {
    const next = name.trim()

    if (!next || next === group.name || busy) {
      setRenaming(false)

      return
    }

    setBusy(true)

    try {
      await updateCntrlGroup(group.name, { name: next })
      setRenaming(false)
    } catch (error) {
      notifyError(error, c.groupNameInvalid)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button aria-label={`${c.actions}: ${label}`} size="icon-xs" variant="ghost">
            <Codicon name="ellipsis" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            onSelect={() =>
              void updateCntrlGroup(group.name, { pinned: !group.pinned }).catch(error => notifyError(error, c.actions))
            }
          >
            {group.pinned ? c.unpinGroup : c.pinGroup}
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => {
              setName(group.name)
              setRenaming(true)
            }}
          >
            {c.rename}
          </DropdownMenuItem>
          <DropdownMenuItem disabled={peerIndex <= 0} onSelect={() => move(-1)}>
            {c.moveUp}
          </DropdownMenuItem>
          <DropdownMenuItem disabled={peerIndex === peers.length - 1} onSelect={() => move(1)}>
            {c.moveDown}
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => {
              void (async () => {
                try {
                  await ungroupAllCntrlGroup(group.name, group.session_ids)
                } catch (error) {
                  notifyError(error, c.ungroupAll)
                }
              })()
            }}
          >
            {c.ungroupAll}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog onOpenChange={setRenaming} open={renaming}>
        <DialogContent>
          <form
            onSubmit={event => {
              event.preventDefault()
              void rename()
            }}
          >
            <DialogHeader>
              <DialogTitle>{c.rename}</DialogTitle>
            </DialogHeader>
            <Input
              aria-label={c.groupName}
              autoFocus
              disabled={busy}
              maxLength={64}
              onChange={event => setName(event.target.value)}
              value={name}
            />
            <DialogFooter>
              <Button onClick={() => setRenaming(false)} type="button" variant="ghost">
                {t.common.cancel}
              </Button>
              <Button disabled={busy} type="submit">
                {t.common.save}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}

interface CntrlGroupSectionProps {
  id: string
  label: string
  group: CntrlGroup | null
  groups: CntrlGroup[]
  sessions: SessionInfo[]
  renderRows: (sessions: SessionInfo[]) => ReactNode
}

/** One cntrl group, drawn exactly like a gateway/profile group: a row-shaped
 *  header (label folds, caret toggles, ⋯ menu) over the section's own rows. */
function CntrlGroupSection({ id, label, group, groups, sessions, renderRows }: CntrlGroupSectionProps) {
  const { t } = useI18n()
  const s = t.sidebar
  const collapsed = useStore($cntrlGroupCollapsed)
  const rankIds = useStore($sidebarSessionRankIds)
  const [visibleCount, setVisibleCount] = useState(SIDEBAR_GROUP_PAGE)
  const open = !collapsed.includes(id)
  const ranked = rankSessions(sessions, rankIds)
  const hiddenCount = Math.max(0, ranked.length - visibleCount)
  const toggle = () => toggleCntrlGroupCollapsed(id)

  return (
    <SidebarRowStack data-cntrl-group={id}>
      <SidebarGroupRow
        actions={group ? <GroupHeaderMenu group={group} groups={groups} label={label} /> : undefined}
        label={
          <SidebarRowLink aria-expanded={open} onClick={toggle}>
            {label}
          </SidebarRowLink>
        }
        lead={
          <SidebarRowLead>
            <SidebarRowLeadGlyph>
              <Codicon name={group?.pinned ? 'pinned' : 'tag'} size={SIDEBAR_LEAD_ICON_SIZE} />
            </SidebarRowLeadGlyph>
          </SidebarRowLead>
        }
        toggle={{ ariaLabel: s.projects.toggle(label, !open), onToggle: toggle, open }}
      />
      {open && (
        <>
          {renderRows(ranked.slice(0, visibleCount))}
          {hiddenCount > 0 && (
            <WorkspaceShowMoreButton
              count={Math.min(SIDEBAR_GROUP_PAGE, hiddenCount)}
              label={label}
              onClick={() => setVisibleCount(count => count + SIDEBAR_GROUP_PAGE)}
            />
          )}
        </>
      )}
    </SidebarRowStack>
  )
}

export interface CntrlGroupRowsProps {
  /** Already ordered: pinned groups first, then by order (orderedCntrlGroups). */
  groups: CntrlGroup[]
  /** The Sessions section's rows (pins already excluded, filters applied). */
  sessions: SessionInfo[]
  renderRows: (sessions: SessionInfo[]) => ReactNode
}

/** The Groups grouping's body inside the Sessions section: one collapsible
 *  group per cntrl group, "Ungrouped" last. A group with no visible rows (all
 *  pinned, archived or filtered out) is left out rather than drawn empty. */
export function CntrlGroupRows({ groups, sessions, renderRows }: CntrlGroupRowsProps) {
  const { t } = useI18n()
  const membership = new Set(groups.flatMap(group => group.session_ids))

  const sections = [
    ...groups.map(group => {
      const ids = new Set(group.session_ids)

      return { group, sessions: sessions.filter(session => ids.has(session.id)) }
    }),
    { group: null, sessions: sessions.filter(session => !membership.has(session.id)) }
  ].filter(section => section.sessions.length > 0)

  return (
    <>
      {sections.map(({ group, sessions: rows }) => {
        const id = group?.name ?? UNGROUPED_ID
        const label = group?.name ?? t.sidebar.gatewayGroups.ungrouped

        return (
          <CntrlGroupSection
            group={group}
            groups={groups}
            id={id}
            key={id}
            label={label}
            renderRows={renderRows}
            sessions={rows}
          />
        )
      })}
    </>
  )
}
