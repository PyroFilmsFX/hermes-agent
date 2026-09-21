import { afterEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/hermes'

import { activeConnection, pluginRest } from './plugins'

// desktop.getConnection/getConnectionFor are IPC round-trips into the main
// process with no timeout of their own (#93454). A wedged main-process
// round-trip must reject instead of hanging pluginSocket's connect() forever.
describe('activeConnection connection timeout (#93454)', () => {
  afterEach(() => {
    setApiRequestConnection(null)
    setApiRequestProfile(null)
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.useRealTimers()
  })

  it('rejects instead of hanging forever when getConnection() wedges', async () => {
    vi.useFakeTimers()
    setApiRequestProfile('coder')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { getConnection: vi.fn(() => new Promise(() => undefined)) }
    })

    const pending = expect(activeConnection()).rejects.toThrow('Timed out connecting to profile "coder"')

    await vi.advanceTimersByTimeAsync(20_000)
    await pending
  })

  it('rejects instead of hanging forever when getConnectionFor() wedges', async () => {
    vi.useFakeTimers()
    setApiRequestConnection('gw-tailscale')
    setApiRequestProfile('research')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnection: vi.fn(() => new Promise(() => undefined)),
        getConnectionFor: vi.fn(() => new Promise(() => undefined))
      }
    })

    const pending = expect(activeConnection()).rejects.toThrow('Timed out connecting to profile "research"')

    await vi.advanceTimersByTimeAsync(20_000)
    await pending
  })
})

describe('pluginRest profile routing', () => {
  afterEach(() => {
    setApiRequestProfile(null)
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  function captureApi() {
    const calls: Array<Record<string, unknown>> = []
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        api: vi.fn(async (request: Record<string, unknown>) => {
          calls.push(request)

          return {}
        })
      }
    })

    return calls
  }

  it("targets the surface's profile when a panel names one, not the active profile", async () => {
    setApiRequestProfile('primary-active')
    const calls = captureApi()

    await pluginRest('conductor', '/state', { profile: 'research' })

    expect(calls.at(-1)).toMatchObject({ path: '/api/plugins/conductor/state', profile: 'research' })
  })

  it('keeps following the active profile when no profile is given', async () => {
    setApiRequestProfile('coder')
    const calls = captureApi()

    await pluginRest('conductor', '/state')

    expect(calls.at(-1)).toMatchObject({ profile: 'coder' })
  })
})
