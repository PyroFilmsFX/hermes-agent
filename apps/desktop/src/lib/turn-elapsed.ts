/**
 * Format elapsed time for a running turn.
 * Outputs:
 * - "<1m" if under 1 minute
 * - "Nm" if under 1 hour (e.g. "1m", "15m", "59m")
 * - "Hh Mm" if 1 hour or more (e.g. "1h 0m", "1h 5m", "2h 30m")
 */
export function formatTurnElapsed(startedAtMs: number, nowMs = Date.now()): string {
  const elapsedMs = Math.max(0, nowMs - startedAtMs)
  const totalMinutes = Math.floor(elapsedMs / 60_000)

  if (totalMinutes < 1) {
    return '<1m'
  }

  if (totalMinutes < 60) {
    return `${totalMinutes}m`
  }

  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60

  return `${hours}h ${minutes}m`
}
