import type { ConductorRow } from '@/api/conductors'
import { pathLeaf } from '@/lib/display-path'
import { type SessionBindingEntry, sessionBindingKey } from '@/store/session-binding'
import type { ProjectInfo } from '@/types/hermes'

/** A row's b10 binding view. Not bound means the row keeps the backend's workspace name. */
export interface RowBinding {
  bound: boolean
  label: null | string
}

export const UNBOUND_ROW: RowBinding = { bound: false, label: null }

const trimRoot = (path: string) => path.trim().replace(/[\\/]+$/, '')

function projectNameForRoot(root: string, projects: readonly ProjectInfo[]): null | string {
  const target = trimRoot(root)

  for (const project of projects) {
    const paths = [project.primary_path, ...(project.folders ?? []).map(folder => folder.path)]

    if (paths.some(path => path && trimRoot(path) === target)) {
      return project.name || null
    }
  }

  return null
}

/**
 * Bound only when main reports a ready, non-refuted bound record. A loading, errored,
 * unbound, needs_reconfirm or unknown session is not bound.
 */
export function rowBinding(
  row: Pick<ConductorRow, 'orchestrator'>,
  bindings: Readonly<Record<string, SessionBindingEntry>>,
  projects: readonly ProjectInfo[]
): RowBinding {
  const sessionId = row.orchestrator.hermes_session_id

  if (!sessionId) {
    return UNBOUND_ROW
  }

  const entry = bindings[sessionBindingKey(row.orchestrator.profile, sessionId)]

  if (!entry || entry.status !== 'ready') {
    return UNBOUND_ROW
  }

  const { record } = entry

  if (record.state !== 'bound' || record.verified === false || !record.project_root) {
    return UNBOUND_ROW
  }

  const label = projectNameForRoot(record.project_root, projects) ?? pathLeaf(record.project_root)

  return label ? { bound: true, label } : UNBOUND_ROW
}
