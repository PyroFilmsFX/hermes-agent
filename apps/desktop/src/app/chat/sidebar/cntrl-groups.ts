import { atom } from 'nanostores'

import { pluginRest } from '@/api/plugins'
import { Codecs, persistentAtom } from '@/lib/persisted'

export interface CntrlGroup {
  name: string
  session_ids: string[]
  pinned: boolean
  order: number
}

export const $cntrlGroups = atom<CntrlGroup[]>([])
/** Whether the cntrl_groups plugin API answers on this connection: null until
 *  the first read settles, false when the plugin is disabled or not installed.
 *  Every Groups affordance (grouping, filter, "Move to group") keys off it. */
export const $cntrlGroupsAvailable = atom<boolean | null>(null)
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

export async function updateCntrlGroup(name: string, patch: { name?: string; order?: number; pinned?: boolean }) {
  await pluginRest('cntrl_groups', `/groups/${encodeURIComponent(name)}`, { method: 'PATCH', body: patch })
  await refreshCntrlGroups()
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
