/** Conductor marker times arrive as epoch seconds or ISO strings; 0 and '' mean "not set". */
export function parseBuildTime(value: number | string | null | undefined): Date | null {
  if (value === null || value === undefined || value === '' || value === 0 || value === '0') {
    return null
  }

  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)

  return Number.isNaN(date.getTime()) ? null : date
}

const CLOCK = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' })

const STAMP = new Intl.DateTimeFormat(undefined, {
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  month: 'short'
})

/** `09:58` */
export const formatClock = (date: Date) => CLOCK.format(date)

/** `Sep 26, 09:58` */
export const formatStamp = (date: Date) => STAMP.format(date)

/** `12 min ago` / `in 3 h`, for tooltips next to an absolute time. */
export function relativeAge(date: Date, nowMs = Date.now()): string {
  const deltaSeconds = Math.round((date.getTime() - nowMs) / 1000)
  const abs = Math.abs(deltaSeconds)
  const [amount, unit] =
    abs < 60 ? [abs, 's'] : abs < 3600 ? [Math.round(abs / 60), 'min'] : abs < 86_400 ? [Math.round(abs / 3600), 'h'] : [Math.round(abs / 86_400), 'd']

  return deltaSeconds <= 0 ? `${amount} ${unit} ago` : `in ${amount} ${unit}`
}

/** `09:58` today, `Sep 25, 09:58` on any other day: a bare clock time would lie about an older day. */
export function sinceTime(date: Date, nowMs = Date.now()): string {
  return new Date(nowMs).toDateString() === date.toDateString() ? formatClock(date) : formatStamp(date)
}

/** `Build idle since 09:58`: the owning session is gone and its lease ran out. */
export function idleSentence(idleSince: number | null | undefined, nowMs = Date.now()): string {
  const date = parseBuildTime(idleSince)

  return date ? `Build idle since ${sinceTime(date, nowMs)}` : 'Build idle'
}

/** `Marker not updated since 09:58`: file age is the last marker write, not activity, so this stays a hint. */
export function markerStaleSentence(staleSince: number | null | undefined, nowMs = Date.now()): string | null {
  const date = parseBuildTime(staleSince)

  return date ? `Marker not updated since ${sinceTime(date, nowMs)}` : null
}

export function leaseExpiredSentence(leaseExpiresAt: number | string | null): string {
  const date = parseBuildTime(leaseExpiresAt)
  const at = date ? ` at ${formatClock(date)}` : ''

  return `The conductor stopped renewing this build${at}. Resume the conductor session to continue.`
}
