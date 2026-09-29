import { atom } from 'nanostores'

import { revealTreePane } from '@/components/pane-shell/tree/store'

/** The pane id every door (palette, build-strip overflow, controller) shares. */
export const CONDUCTORS_PANE_ID = 'conductors'

/** Summoned-only, like the per-session `conductor` pane: the controller
 *  registers the pane while this is true and removes it when false. Kept out
 *  of the pane module so the composer's build strip can open the page
 *  without importing the table. */
export const $conductorsPaneOpen = atom(false)

export function openConductorsPane(): void {
  if ($conductorsPaneOpen.get()) {
    revealTreePane(CONDUCTORS_PANE_ID)
  } else {
    $conductorsPaneOpen.set(true)
  }
}
