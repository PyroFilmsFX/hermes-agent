import { atom } from 'nanostores'
import type { ConductorBuild as ConductorBuildWire, ConductorBuildResult } from '@hermes/shared'

export type ConductorBuild = ConductorBuildWire
export type ConductorBuildState = ConductorBuild['state']

export interface ConductorBuildSnapshot extends Pick<ConductorBuildResult, 'build'> {
  unreadable: boolean
}

export const $conductorBuildBySession = atom<Record<string, ConductorBuild | null>>({})

/** An unreadable marker is transient, so it must not erase the last good read. */
export function reconcileConductorBuildSnapshot(sessionId: string, snapshot: ConductorBuildSnapshot) {
  if (snapshot.unreadable) {
    return
  }

  $conductorBuildBySession.set({
    ...$conductorBuildBySession.get(),
    [sessionId]: snapshot.build
  })
}
