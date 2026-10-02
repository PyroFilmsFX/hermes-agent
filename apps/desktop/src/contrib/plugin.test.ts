import { describe, expect, it, vi } from 'vitest'

import { dispatchPluginNativeNotification } from '@/store/native-notifications'

import { emitGatewayEvent, onGatewayEvent } from './events'
import { createPluginContext } from './plugin'

vi.mock('@/store/native-notifications', () => ({ dispatchPluginNativeNotification: vi.fn() }))

describe('createPluginContext.onDispose', () => {
  it('collects arbitrary cleanups so the host runs them on deactivate', () => {
    const disposers: Array<() => void> = []
    const ctx = createPluginContext('demo', dispose => disposers.push(dispose))

    let cleaned = false
    ctx.onDispose(() => {
      cleaned = true
    })

    // The cleanup is tracked alongside contribution/socket disposers, so the
    // loader's deactivate (which runs every collected disposer) tears it down.
    expect(disposers).toHaveLength(1)
    disposers.forEach(dispose => dispose())
    expect(cleaned).toBe(true)
  })
})

describe('createPluginContext.onEvent', () => {
  it('retires the gateway listener with the plugin, even when subscribed after register()', () => {
    // Runtime plugins are re-imported as a fresh module on every hot reload; a
    // gateway subscription the loader cannot see outlives its incarnation and
    // one event then fires once per reload (#112366). The ctx door is tracked.
    const disposers: Array<() => void> = []
    const ctx = createPluginContext('demo', dispose => disposers.push(dispose))
    const seen: string[] = []

    // Deliberately outside any loader register() scope — the late-subscription case.
    ctx.onEvent('gateway.reconnecting', event => seen.push(event.type))
    emitGatewayEvent({ type: 'gateway.reconnecting', payload: { attempt: 1 } } as never)
    expect(seen).toEqual(['gateway.reconnecting'])

    disposers.forEach(dispose => dispose())
    emitGatewayEvent({ type: 'gateway.reconnecting', payload: { attempt: 2 } } as never)
    expect(seen).toEqual(['gateway.reconnecting'])
  })
})

describe('createPluginContext.os', () => {
  it('dispatches a native notification attributed to the plugin', () => {
    const ctx = createPluginContext('demo')
    ctx.os.notify({ body: 'b', title: 't' })
    expect(dispatchPluginNativeNotification).toHaveBeenCalledWith('demo', { body: 'b', title: 't' })
  })

  it('resolves false (never throws) when the desktop bridge is missing', async () => {
    const ctx = createPluginContext('demo')

    // jsdom has no window.hermesDesktop — the exact older-shell/browser case.
    await expect(ctx.os.openExternal('https://example.com')).resolves.toBe(false)
    await expect(ctx.os.revealPath('/tmp')).resolves.toBe(false)
    await expect(ctx.os.writeClipboard('hi')).resolves.toBe(false)
    // The pickers answer with a path, so their "unavailable" is null.
    await expect(ctx.os.pickSavePath()).resolves.toBeNull()
    await expect(ctx.os.pickOpenPath()).resolves.toBeNull()
  })

  it('file pickers return the chosen path, and null on cancel', async () => {
    const bridge = {
      selectPaths: vi.fn().mockResolvedValue(['/tmp/board.tar.gz']),
      selectSavePath: vi.fn().mockResolvedValue('/tmp/out.tar.gz')
    }

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = bridge

    try {
      const ctx = createPluginContext('demo')

      await expect(ctx.os.pickSavePath({ title: 'Save' })).resolves.toBe('/tmp/out.tar.gz')
      expect(bridge.selectSavePath).toHaveBeenCalledWith({ title: 'Save' })

      await expect(ctx.os.pickOpenPath({ title: 'Open' })).resolves.toBe('/tmp/board.tar.gz')
      expect(bridge.selectPaths).toHaveBeenCalledWith({ multiple: false, title: 'Open' })

      // Cancel: the save dialog resolves null, the open dialog an empty list.
      bridge.selectSavePath.mockResolvedValue(null)
      bridge.selectPaths.mockResolvedValue([])
      await expect(ctx.os.pickSavePath()).resolves.toBeNull()
      await expect(ctx.os.pickOpenPath()).resolves.toBeNull()
    } finally {
      delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    }
  })

  it('file pickers degrade to null on an older shell that lacks them', async () => {
    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {}

    try {
      const ctx = createPluginContext('demo')
      await expect(ctx.os.pickSavePath()).resolves.toBeNull()
      await expect(ctx.os.pickOpenPath()).resolves.toBeNull()
    } finally {
      delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    }
  })

  it('routes through the bridge and turns a bridge throw into false', async () => {
    const bridge = {
      openExternal: vi.fn().mockResolvedValue(undefined),
      revealPath: vi.fn().mockResolvedValue(true),
      writeClipboard: vi.fn().mockRejectedValue(new Error('nope'))
    }

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = bridge

    try {
      const ctx = createPluginContext('demo')
      await expect(ctx.os.openExternal('https://example.com')).resolves.toBe(true)
      expect(bridge.openExternal).toHaveBeenCalledWith('https://example.com')
      await expect(ctx.os.revealPath('/tmp')).resolves.toBe(true)
      await expect(ctx.os.writeClipboard('hi')).resolves.toBe(false)
    } finally {
      delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    }
  })
})

describe('M11: plugin.event bridge to desktop plugins host.onEvent', () => {
  it('dispatches event to matching handler only', () => {
    const matchHandler = vi.fn()
    const otherHandler = vi.fn()

    const offMatch = onGatewayEvent('task.completed', matchHandler)
    const offOther = onGatewayEvent('task.failed', otherHandler)

    try {
      emitGatewayEvent({
        type: 'plugin.event',
        payload: {
          plugin: 'kanban',
          name: 'task.completed',
          payload: { taskId: 't-1', status: 'done' }
        }
      } as never)

      expect(matchHandler).toHaveBeenCalledTimes(1)
      expect(matchHandler).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'plugin.event',
          payload: {
            plugin: 'kanban',
            name: 'task.completed',
            payload: { taskId: 't-1', status: 'done' }
          }
        })
      )
      expect(otherHandler).not.toHaveBeenCalled()
    } finally {
      offMatch()
      offOther()
    }
  })

  it('isolates throwing handlers so other handlers still run', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const brokenHandler = vi.fn(() => {
      throw new Error('handler crashed')
    })
    const healthyHandler = vi.fn()

    const offBroken = onGatewayEvent('sync.event', brokenHandler)
    const offHealthy = onGatewayEvent('sync.event', healthyHandler)

    try {
      emitGatewayEvent({
        type: 'plugin.event',
        payload: {
          plugin: 'sync-plugin',
          name: 'sync.event',
          payload: { count: 5 }
        }
      } as never)

      expect(brokenHandler).toHaveBeenCalledTimes(1)
      expect(healthyHandler).toHaveBeenCalledTimes(1)
      expect(errorSpy).toHaveBeenCalled()
    } finally {
      offBroken()
      offHealthy()
      errorSpy.mockRestore()
    }
  })

  it('unsubscribes handler on plugin unload', () => {
    const disposers: Array<() => void> = []
    const ctx = createPluginContext('lifecycle-plugin', d => disposers.push(d))
    const handler = vi.fn()

    ctx.onEvent('item.updated', handler)

    emitGatewayEvent({
      type: 'plugin.event',
      payload: {
        plugin: 'lifecycle-plugin',
        name: 'item.updated',
        payload: { id: 10 }
      }
    } as never)

    expect(handler).toHaveBeenCalledTimes(1)

    disposers.forEach(d => d())

    emitGatewayEvent({
      type: 'plugin.event',
      payload: {
        plugin: 'lifecycle-plugin',
        name: 'item.updated',
        payload: { id: 20 }
      }
    } as never)

    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('completes direct dispatch within 1 second', () => {
    const handler = vi.fn()
    const off = onGatewayEvent('speed.test', handler)

    try {
      const start = performance.now()
      emitGatewayEvent({
        type: 'plugin.event',
        payload: {
          plugin: 'perf-plugin',
          name: 'speed.test',
          payload: { ts: Date.now() }
        }
      } as never)
      const elapsed = performance.now() - start

      expect(handler).toHaveBeenCalledTimes(1)
      expect(elapsed).toBeLessThan(1000)
    } finally {
      off()
    }
  })
})
