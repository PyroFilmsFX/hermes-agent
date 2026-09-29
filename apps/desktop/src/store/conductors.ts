import { atom } from 'nanostores'

import {
  fetchConductors,
  type ConductorsResponse,
  type FetchConductorsOptions
} from '@/api/conductors'

export const FOCUSED_POLL_INTERVAL_MS = 15_000
export const BLURRED_POLL_INTERVAL_MS = 60_000
export const MAX_CONSECUTIVE_FAILURES = 3

export type ConductorsStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface ConductorsState {
  status: ConductorsStatus
  data: ConductorsResponse | null
  fetchedAt: number | null
  failures: number
}

export const $conductors = atom<ConductorsState>({
  status: 'idle',
  data: null,
  fetchedAt: null,
  failures: 0
})

let pollerUsers = 0
let pollerTimer: ReturnType<typeof setInterval> | null = null
let pollerInterval: number | null = null
let inFlight: Promise<ConductorsResponse | null> | null = null
let isListening = false

function isDocumentVisible(): boolean {
  if (typeof document === 'undefined') {
    return true
  }
  return document.visibilityState === 'visible'
}

function isWindowFocused(): boolean {
  if (typeof document === 'undefined') {
    return true
  }
  if (typeof document.hasFocus === 'function') {
    return document.hasFocus()
  }
  return true
}

function shouldPoll(): boolean {
  return pollerUsers > 0 && isDocumentVisible() && $conductors.get().failures < MAX_CONSECUTIVE_FAILURES
}

function clearPollerTimer(): void {
  if (pollerTimer !== null) {
    clearInterval(pollerTimer)
    pollerTimer = null
  }
  pollerInterval = null
}

function ensurePollerTimer(): void {
  if (!shouldPoll()) {
    clearPollerTimer()
    return
  }

  const desiredInterval = isWindowFocused() ? FOCUSED_POLL_INTERVAL_MS : BLURRED_POLL_INTERVAL_MS

  if (pollerTimer !== null && pollerInterval === desiredInterval) {
    return
  }

  clearPollerTimer()
  pollerInterval = desiredInterval
  pollerTimer = setInterval(() => {
    if (!shouldPoll()) {
      clearPollerTimer()
      return
    }
    void refreshConductors()
  }, desiredInterval)
}

const onVisibilityChange = (): void => {
  if (!isDocumentVisible()) {
    clearPollerTimer()
    return
  }

  if ($conductors.get().failures >= MAX_CONSECUTIVE_FAILURES) {
    $conductors.set({
      ...$conductors.get(),
      failures: 0
    })
  }

  void refreshConductors()
  ensurePollerTimer()
}

const onFocus = (): void => {
  if (!isDocumentVisible()) {
    return
  }

  if ($conductors.get().failures >= MAX_CONSECUTIVE_FAILURES) {
    $conductors.set({
      ...$conductors.get(),
      failures: 0
    })
    void refreshConductors()
  }

  ensurePollerTimer()
}

const onBlur = (): void => {
  if (!isDocumentVisible()) {
    return
  }
  ensurePollerTimer()
}

function startListening(): void {
  if (isListening || typeof window === 'undefined') {
    return
  }
  isListening = true
  document.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('focus', onFocus)
  window.addEventListener('blur', onBlur)
}

function stopListening(): void {
  if (!isListening || typeof window === 'undefined') {
    return
  }
  isListening = false
  document.removeEventListener('visibilitychange', onVisibilityChange)
  window.removeEventListener('focus', onFocus)
  window.removeEventListener('blur', onBlur)
}

export function acquireConductorsPoller(): () => void {
  pollerUsers += 1

  if (pollerUsers === 1) {
    startListening()
    if (isDocumentVisible()) {
      void refreshConductors()
      ensurePollerTimer()
    }
  }

  let released = false
  return () => {
    if (released) {
      return
    }
    released = true
    pollerUsers -= 1

    if (pollerUsers === 0) {
      clearPollerTimer()
      stopListening()
    }
  }
}

export async function refreshConductors(options: FetchConductorsOptions = {}): Promise<ConductorsResponse | null> {
  if (inFlight) {
    return inFlight
  }

  const current = $conductors.get()
  if (current.status === 'idle') {
    $conductors.set({ ...current, status: 'loading' })
  }

  inFlight = (async () => {
    try {
      const data = await fetchConductors(options)
      $conductors.set({
        status: 'ready',
        data,
        fetchedAt: Date.now(),
        failures: 0
      })
      ensurePollerTimer()
      return data
    } catch {
      const state = $conductors.get()
      const nextFailures = state.failures + 1
      $conductors.set({
        status: 'error',
        data: state.data,
        fetchedAt: state.fetchedAt,
        failures: nextFailures
      })
      if (nextFailures >= MAX_CONSECUTIVE_FAILURES) {
        clearPollerTimer()
      }
      return null
    }
  })().finally(() => {
    inFlight = null
  })

  return inFlight
}

export function _resetConductorsPollerForTest(): void {
  clearPollerTimer()
  stopListening()
  pollerUsers = 0
  inFlight = null
  $conductors.set({
    status: 'idle',
    data: null,
    fetchedAt: null,
    failures: 0
  })
}
