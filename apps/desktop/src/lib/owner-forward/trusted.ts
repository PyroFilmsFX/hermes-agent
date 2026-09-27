/**
 * #60 owner-forward: the renderer's trusted-gesture filter. `isTrusted` is false for script-dispatched
 * events (widgets, `::preview` JS, `el.click()`), so they can't open the Forward sheet or press Send.
 * This is a cheap filter, not the proof: main's native confirm is (design §3.4).
 */
export function isTrustedGesture(event: { isTrusted?: boolean } | null | undefined): boolean {
  return event?.isTrusted === true
}
