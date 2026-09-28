import { Codecs, persistentAtom } from '@/lib/persisted'

/** Keep the list bounded: one entry per (session, from, to) the user waved off. */
const MAX_DISMISSALS = 500

/**
 * Upgrade hints (D53) the user dismissed, per session AND model pair. Desktop-
 * local like the other per-session UI prefs (pins, colors). Keyed on the
 * target too, so a LATER release (Opus 5 → Opus 6) surfaces again even after
 * "Opus 5 → Opus 5.5" was dismissed for that session.
 */
export const $dismissedModelUpgrades = persistentAtom<string[]>(
  'hermes.desktop.dismissedModelUpgrades',
  [],
  Codecs.stringArray
)

/** Stable key for one dismissed hint. `scope` is the session's durable id
 *  (stored id, else runtime id, else `draft`). */
export const modelUpgradeDismissKey = (scope: string, provider: string, from: string, to: string): string =>
  [scope, provider, from, to].join('::')

export function isModelUpgradeDismissed(key: string, dismissed: readonly string[] = $dismissedModelUpgrades.get()) {
  return dismissed.includes(key)
}

export function dismissModelUpgrade(key: string): void {
  const prev = $dismissedModelUpgrades.get()

  if (prev.includes(key)) {
    return
  }

  $dismissedModelUpgrades.set([...prev, key].slice(-MAX_DISMISSALS))
}
