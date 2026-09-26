import { modelDisplayParts } from '@/lib/model-status-label'

/**
 * One vocabulary for conductor workers on every surface (composer stack, Conductor pane,
 * worker output pane). Seats are neutral monograms: colour is reserved for state.
 */
export type Seat = 'agy' | 'claude' | 'codex' | 'grok' | 'other'

export type StatusTone = 'attention' | 'idle' | 'live' | 'ok' | 'stop' | 'wait'

const SEAT_MONOGRAM: Record<Exclude<Seat, 'other'>, string> = { agy: 'ag', claude: 'cl', codex: 'cx', grok: 'gk' }
const SEAT_LABEL: Record<Exclude<Seat, 'other'>, string> = { agy: 'agy', claude: 'Claude', codex: 'Codex', grok: 'Grok' }

const UNATTESTED = new Set(['', 'unverified', 'backend-does-not-attest'])

export function seatFor(worker: string, model = ''): Seat {
  const name = worker.trim().toLowerCase()

  if (name === 'codex') {
    return 'codex'
  }

  if (name === 'agy' || name === 'gemini') {
    return 'agy'
  }

  if (name === 'grok') {
    return 'grok'
  }

  if (name === 'claude' || /^claude-/i.test(model.trim())) {
    return 'claude'
  }

  return 'other'
}

export function seatMonogram(worker: string, model = ''): string {
  const seat = seatFor(worker, model)

  if (seat !== 'other') {
    return SEAT_MONOGRAM[seat]
  }

  return worker.replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toLowerCase() || '?'
}

export function seatLabel(worker: string, model = ''): string {
  const seat = seatFor(worker, model)

  return seat === 'other' ? worker.trim() : SEAT_LABEL[seat]
}

const capitalize = (word: string) => (word ? word.charAt(0).toUpperCase() + word.slice(1) : word)

/** `gpt-6-luna` → `GPT-6 Luna`; `gemini-3.8-flash-medium` (effort medium) → `Gemini 3.8 Flash`. */
export function modelLabel(model: string, effort?: string): string {
  let id = model.trim()

  if (UNATTESTED.has(id.toLowerCase())) {
    return ''
  }

  const suffix = effort?.trim() ? `-${effort.trim()}` : ''

  if (suffix && id.toLowerCase().endsWith(suffix.toLowerCase()) && id.length > suffix.length) {
    id = id.slice(0, -suffix.length)
  }

  const name = modelDisplayParts(id).name
  const gpt = name.match(/^GPT-[\w.]+/)

  if (gpt) {
    const rest = name.slice(gpt[0].length).split(/[-\s]+/).filter(Boolean).map(capitalize)

    return [gpt[0], ...rest].join(' ')
  }

  return name.split(/[-\s]+/).filter(Boolean).map(capitalize).join(' ')
}

const KIND_LABEL: Record<string, string> = {
  council: 'Council',
  fix: 'Fix',
  impl: 'Build',
  research: 'Research',
  validate: 'Review'
}

export function kindLabel(lane: string): string {
  const key = lane.trim().toLowerCase()

  return KIND_LABEL[key] ?? capitalize(key)
}

const TONE_BY_STATUS: Record<string, StatusTone> = {
  active: 'live',
  answered: 'ok',
  blocked: 'stop',
  canceled: 'stop',
  cancelled: 'stop',
  completed: 'ok',
  done: 'ok',
  error: 'stop',
  failed: 'stop',
  idle: 'idle',
  lease_expired: 'attention',
  running: 'live',
  stale: 'attention',
  succeeded: 'ok',
  timeout: 'attention',
  waiting: 'wait'
}

const LABEL_BY_STATUS: Record<string, string> = {
  active: 'Running',
  answered: 'Answered',
  blocked: 'Blocked',
  canceled: 'Cancelled',
  cancelled: 'Cancelled',
  completed: 'Done',
  done: 'Done',
  error: 'Failed',
  failed: 'Failed',
  idle: 'Idle',
  lease_expired: 'Lease expired',
  running: 'Running',
  stale: 'Not responding',
  succeeded: 'Done',
  timeout: 'Timed out',
  waiting: 'Waiting'
}

/** Unknown statuses read as needing attention, never as blank or as fine. */
export function statusTone(status: string): StatusTone {
  return TONE_BY_STATUS[status.trim().toLowerCase()] ?? 'attention'
}

export function statusLabel(status: string, lane = ''): string {
  const key = status.trim().toLowerCase()

  if (lane.trim().toLowerCase() === 'council' && statusTone(key) === 'ok') {
    return 'Answered'
  }

  return LABEL_BY_STATUS[key] ?? (capitalize(key.replace(/_/g, ' ')) || 'Unknown')
}

export const isTerminalStatus = (status: string) => !['running', 'active', 'waiting'].includes(status)

export const needsAttention = (status: string) => ['attention', 'stop'].includes(statusTone(status))

/** Only when the exit adds information: a signal-killed timeout is not a result code. */
export function exitNote(status: string, exitCode: number | undefined): string | null {
  if (status === 'timeout') {
    return 'stopped after the time limit'
  }

  if (status === 'failed' || status === 'error') {
    return exitCode === undefined ? 'no exit code' : exitCode === 0 ? null : `exit ${exitCode}`
  }

  return null
}
