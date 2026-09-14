import { describe, expect, it } from 'vitest'

import {
  apiErrorFromEnvelope,
  isApiErrorEnvelope,
  isQuietApiFailure,
  toApiErrorEnvelope,
  withQuietApiFailures
} from './api-error-envelope'

/** The error shape api-transport's httpStatusError produces. */
function httpError(statusCode: number, detail: string) {
  const error: Error & { statusCode?: number } = new Error(`${statusCode}: ${detail}`)
  error.statusCode = statusCode

  return error
}

describe('quiet hermes:api failures', () => {
  it('round-trips a 404 into the byte-identical rejection Electron would have produced', async () => {
    const original = httpError(404, '{"detail":"Session not found"}')

    const result = await withQuietApiFailures(() => Promise.reject(original))

    expect(isApiErrorEnvelope(result)).toBe(true)
    const rebuilt = apiErrorFromEnvelope(result as never)
    // This exact string is what session-gone-latch.ts and inlineErrorMessage()
    // parse; drift here silently breaks session-gone latching.
    expect(rebuilt.message).toBe(
      'Error invoking remote method \'hermes:api\': Error: 404: {"detail":"Session not found"}'
    )
    expect((rebuilt as Error & { statusCode?: number }).statusCode).toBe(404)
  })

  it('the rebuilt error still satisfies the renderer session-gone matcher', () => {
    const rebuilt = apiErrorFromEnvelope(toApiErrorEnvelope(httpError(404, '{"detail":"Session not found"}')))
    // Mirrors session-gone-latch.ts: strip Electron's prefix, then match.
    const message = rebuilt.message.replace(/^Error invoking remote method '[^']+':\s*Error:\s*/i, '')
    expect(/^404\b/.test(message) && /session not found/i.test(message)).toBe(true)
  })

  it('passes a successful result straight through', async () => {
    await expect(withQuietApiFailures(() => Promise.resolve({ ok: true }))).resolves.toEqual({ ok: true })
  })

  it('rethrows anything not on the quiet allowlist, so real faults stay loud', async () => {
    for (const status of [500, 503, 401, 403, 409]) {
      expect(isQuietApiFailure(httpError(status, 'boom'))).toBe(false)
      await expect(withQuietApiFailures(() => Promise.reject(httpError(status, 'boom')))).rejects.toThrow(
        `${status}: boom`
      )
    }
  })

  it('rethrows an error carrying no status at all', async () => {
    await expect(withQuietApiFailures(() => Promise.reject(new Error('socket hang up')))).rejects.toThrow(
      'socket hang up'
    )
  })

  it('does not mistake an ordinary payload for an envelope', () => {
    for (const value of [null, undefined, {}, { message: 'x' }, 'str', 404]) {
      expect(isApiErrorEnvelope(value)).toBe(false)
    }
  })
})
