import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'

import { callMcpAppTool, readMcpAppResource } from './resolve'

describe('callMcpAppTool', () => {
  beforeEach(() => {
    $activeSessionId.set(null)
    $gateway.set(null as any)
  })

  it('refuses without a session id and never falls back to the focused chat', async () => {
    const mockRequest = vi.fn()
    $gateway.set({ request: mockRequest } as any)
    $activeSessionId.set('session-active-123')

    await expect(
      callMcpAppTool({ appUri: 'ui://w/app', server: 'w', name: 'get_forecast', arguments: {} })
    ).rejects.toThrow(/not attached to a session/)
    await expect(
      callMcpAppTool({ appUri: 'ui://w/app', server: 'w', name: 'get_forecast', arguments: {} }, null)
    ).rejects.toThrow(/not attached to a session/)
    expect(mockRequest).not.toHaveBeenCalled()
  })

  it('passes explicit session id override', async () => {
    const mockRequest = vi.fn().mockResolvedValue({
      content: [{ type: 'text', text: 'ok' }]
    })
    $gateway.set({ request: mockRequest } as any)
    $activeSessionId.set('session-active-123')

    await callMcpAppTool(
      {
        appUri: 'ui://weather-server/app',
        server: 'weather-server',
        name: 'get_forecast',
        arguments: { city: 'Tokyo' }
      },
      'session-explicit-456'
    )

    expect(mockRequest).toHaveBeenCalledWith('mcp.tools.call', {
      session_id: 'session-explicit-456',
      server: 'weather-server',
      name: 'get_forecast',
      arguments: { city: 'Tokyo' }
    })
  })
})

describe('readMcpAppResource', () => {
  it('names its server in the read request', async () => {
    const mockRequest = vi
      .fn()
      .mockResolvedValue({ server: 'weather-server', uri: 'ui://weather-server/app', contents: [] })
    $gateway.set({ request: mockRequest } as any)

    await readMcpAppResource('weather-server', 'ui://weather-server/app')

    expect(mockRequest).toHaveBeenCalledWith('mcp.resources.read', {
      server: 'weather-server',
      uri: 'ui://weather-server/app'
    })
  })
})
