/**
 * Quiet transport for EXPECTED `hermes:api` failures.
 *
 * Electron logs a full stack for every rejection out of an `ipcMain.handle`
 * callback ("Error occurred in handler for 'hermes:api'"). A 404 for a session
 * the renderer is still polling is an ordinary, self-limiting outcome — the
 * renderer latches it and stops (see src/store/session-gone-latch.ts) — but
 * each one still printed a multi-line stack, which buries real errors in the
 * dev log (observed 2026-09-14).
 *
 * So the main process RESOLVES with an envelope for those statuses instead of
 * rejecting, and the preload turns the envelope back into a rejection whose
 * message is BYTE-IDENTICAL to the one Electron would have produced — prefix
 * included, because both `session-gone-latch.ts` and `inlineErrorMessage()`
 * parse that prefix. Nothing downstream can tell the difference; only the
 * main-process log gets quieter.
 *
 * Statuses stay a tight allowlist: anything unexpected keeps rejecting (and
 * keeps its loud log), which is what we want for a genuine fault.
 */

/** HTTP statuses the renderer is expected to handle without a stack trace. */
const QUIET_STATUSES = new Set([404])

const ENVELOPE_KEY = '__hermesApiExpectedError'

export type ApiErrorEnvelope = {
  [ENVELOPE_KEY]: true
  message: string
  statusCode: number
}

function statusOf(error: unknown): number {
  return Number(error && typeof error === 'object' ? (error as { statusCode?: unknown }).statusCode : NaN)
}

/** True when this failure is routine enough to travel as a resolved envelope. */
export function isQuietApiFailure(error: unknown): boolean {
  return QUIET_STATUSES.has(statusOf(error))
}

export function toApiErrorEnvelope(error: unknown, channel = 'hermes:api'): ApiErrorEnvelope {
  const inner = error instanceof Error ? `Error: ${error.message}` : String(error ?? '')

  return {
    [ENVELOPE_KEY]: true,
    // Exactly what Electron's own rejection would have read.
    message: `Error invoking remote method '${channel}': ${inner}`,
    statusCode: statusOf(error)
  }
}

export function isApiErrorEnvelope(value: unknown): value is ApiErrorEnvelope {
  return Boolean(value && typeof value === 'object' && (value as Record<string, unknown>)[ENVELOPE_KEY] === true)
}

/** Preload side: rebuild the rejection the renderer already knows how to read. */
export function apiErrorFromEnvelope(envelope: ApiErrorEnvelope): Error {
  const error: Error & { statusCode?: number } = new Error(envelope.message)
  if (Number.isInteger(envelope.statusCode)) {
    error.statusCode = envelope.statusCode
  }

  return error
}

/**
 * Run an `ipcMain.handle` body, converting expected failures into envelopes.
 * Unexpected failures rethrow untouched.
 */
export async function withQuietApiFailures<T>(run: () => Promise<T> | T, channel = 'hermes:api'): Promise<ApiErrorEnvelope | T> {
  try {
    return await run()
  } catch (error) {
    if (isQuietApiFailure(error)) {
      return toApiErrorEnvelope(error, channel)
    }

    throw error
  }
}
