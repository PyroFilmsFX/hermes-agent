/** Which session a contribution is rendering for.
 *
 * Contributions render as components inside a `Slot`, with no arguments — fine for a global chip,
 * useless for a panel that must answer "is THIS session inside a build, and how far along?". A tile
 * IS its session, and a tile's session can live on another profile than the primary, so a plugin
 * panel needs both to ask its own backend the right question (and to address the right backend at
 * all — see `PluginRestOptions.profile`).
 *
 * Deliberately tiny and read-only: the surface publishes identity, never control. Rendering outside
 * a provider (a statusbar chip, a titlebar item) yields nulls rather than throwing, so the same
 * contribution can render in several places.
 */

import { createContext, type ReactNode, useContext, useMemo } from 'react'

export interface ContribSurface {
  /** Live session id this surface renders for, or null when it has no session. */
  sessionId: null | string
  /** Gateway profile that session belongs to, or null for the primary. */
  profile: null | string
}

const EMPTY_SURFACE: ContribSurface = { profile: null, sessionId: null }

const ContribSurfaceContext = createContext<ContribSurface>(EMPTY_SURFACE)

export function ContribSurfaceProvider({
  children,
  profile = null,
  sessionId
}: {
  children: ReactNode
  profile?: null | string
  sessionId: null | string
}) {
  const value = useMemo<ContribSurface>(() => ({ profile, sessionId }), [profile, sessionId])

  return <ContribSurfaceContext.Provider value={value}>{children}</ContribSurfaceContext.Provider>
}

/** The session/profile this contribution renders for; nulls outside a session surface. */
export function useContribSurface(): ContribSurface {
  return useContext(ContribSurfaceContext)
}
