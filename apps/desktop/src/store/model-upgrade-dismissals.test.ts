import { beforeEach, describe, expect, it, vi } from 'vitest'

const STORAGE_KEY = 'hermes.desktop.dismissedModelUpgrades'

describe('model upgrade dismissals', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('persists a dismissal across a reload, per session and model pair', async () => {
    const first = await import('./model-upgrade-dismissals')
    const key = first.modelUpgradeDismissKey('session-a', 'anthropic', 'claude-opus-5', 'claude-opus-5-5')

    expect(first.isModelUpgradeDismissed(key)).toBe(false)
    first.dismissModelUpgrade(key)
    expect(first.isModelUpgradeDismissed(key)).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '[]')).toEqual([key])

    // A fresh module instance reads the stored record back (app relaunch).
    vi.resetModules()
    const reloaded = await import('./model-upgrade-dismissals')

    expect(reloaded.isModelUpgradeDismissed(key)).toBe(true)
    // Another session, or a LATER target for the same session, is not dismissed.
    expect(
      reloaded.isModelUpgradeDismissed(
        reloaded.modelUpgradeDismissKey('session-b', 'anthropic', 'claude-opus-5', 'claude-opus-5-5')
      )
    ).toBe(false)
    expect(
      reloaded.isModelUpgradeDismissed(
        reloaded.modelUpgradeDismissKey('session-a', 'anthropic', 'claude-opus-5', 'claude-opus-6')
      )
    ).toBe(false)
  })

  it('does not duplicate a repeated dismissal', async () => {
    const store = await import('./model-upgrade-dismissals')
    const key = store.modelUpgradeDismissKey('s', 'openai', 'gpt-5.5', 'gpt-6')

    store.dismissModelUpgrade(key)
    store.dismissModelUpgrade(key)

    expect(store.$dismissedModelUpgrades.get()).toEqual([key])
  })
})
