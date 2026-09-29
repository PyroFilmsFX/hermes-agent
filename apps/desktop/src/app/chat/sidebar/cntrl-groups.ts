import { atom } from 'nanostores'

import { pluginRest } from '@/api/plugins'
import { Codecs, persistentAtom } from '@/lib/persisted'
import { $cntrlGroupsAvailable, $sidebarGroupFilter, setSidebarGroupFilter } from '@/store/layout'

export { $cntrlGroupsAvailable }

export const CNTRL_GROUP_UNGROUPED = '/ungrouped'
export const CNTRL_GROUP_UNGROUPED_ID = CNTRL_GROUP_UNGROUPED
export const UNGROUPED_ID = CNTRL_GROUP_UNGROUPED

export interface CntrlGroup {
  name: string
  session_ids: string[]
  pinned: boolean
  order: number
}

export const $cntrlGroups = atom<CntrlGroup[]>([])
export const $cntrlGroupCollapsed = persistentAtom(
  'hermes.desktop.sidebar.cntrlGroups.collapsed.v1',
  [],
  Codecs.stringArray
)
let refreshGeneration = 0

export async function refreshCntrlGroups() {
  const generation = ++refreshGeneration

  try {
    const groups = await pluginRest<CntrlGroup[]>('cntrl_groups', '/groups')

    if (generation === refreshGeneration) {
      $cntrlGroups.set(Array.isArray(groups) ? groups : [])
      $cntrlGroupsAvailable.set(Array.isArray(groups))
    }
  } catch {
    if (generation === refreshGeneration) {
      $cntrlGroups.set([])
      $cntrlGroupsAvailable.set(false)
    }
  }
}

export async function tagCntrlGroup(name: string, sessionId: string) {
  await pluginRest('cntrl_groups', `/groups/${encodeURIComponent(name)}/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PUT'
  })
  await refreshCntrlGroups()
}

export async function untagCntrlGroup(name: string, sessionId: string, refresh = true) {
  await pluginRest('cntrl_groups', `/groups/${encodeURIComponent(name)}/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE'
  })

  if (refresh) {
    await refreshCntrlGroups()
  }
}

export function migrateCntrlGroup(oldName: string, newName: string) {
  if ($sidebarGroupFilter.get() === oldName) {
    setSidebarGroupFilter(newName)
  }

  const current = $cntrlGroupCollapsed.get()

  if (current.includes(oldName)) {
    $cntrlGroupCollapsed.set(current.map(item => (item === oldName ? newName : item)))
  }
}

export function clearCntrlGroup(name: string) {
  if ($sidebarGroupFilter.get() === name) {
    setSidebarGroupFilter(null)
  }

  const current = $cntrlGroupCollapsed.get()

  if (current.includes(name)) {
    $cntrlGroupCollapsed.set(current.filter(item => item !== name))
  }
}

export async function updateCntrlGroup(name: string, patch: { name?: string; order?: number; pinned?: boolean }) {
  await pluginRest('cntrl_groups', `/groups/${encodeURIComponent(name)}`, { method: 'PATCH', body: patch })

  if (patch.name && patch.name !== name) {
    migrateCntrlGroup(name, patch.name)
  }

  await refreshCntrlGroups()
}

export async function ungroupAllCntrlGroup(name: string, sessionIds: string[]) {
  try {
    for (const id of sessionIds) {
      await untagCntrlGroup(name, id, false)
    }
  } finally {
    clearCntrlGroup(name)
    await refreshCntrlGroups()
  }
}

/** Persist a band's order as 0..n-1 in the given sequence, then read back once. */
export async function reorderCntrlGroups(names: string[], groups: CntrlGroup[]) {
  const current = new Map(groups.map(group => [group.name, group.order]))

  try {
    await Promise.all(
      names.map((name, order) =>
        current.get(name) === order
          ? null
          : pluginRest('cntrl_groups', `/groups/${encodeURIComponent(name)}`, { method: 'PATCH', body: { order } })
      )
    )
  } finally {
    await refreshCntrlGroups()
  }
}

export function toggleCntrlGroupCollapsed(name: string) {
  const current = $cntrlGroupCollapsed.get()
  $cntrlGroupCollapsed.set(current.includes(name) ? current.filter(item => item !== name) : [...current, name])
}

export function orderedCntrlGroups(groups: CntrlGroup[]) {
  return [...groups].sort(
    (left, right) =>
      Number(right.pinned) - Number(left.pinned) || left.order - right.order || left.name.localeCompare(right.name)
  )
}
