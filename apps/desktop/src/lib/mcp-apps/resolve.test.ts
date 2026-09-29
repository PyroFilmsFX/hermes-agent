import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'

import { callMcpAppTool } from './resolve'

describe('callMcpAppTool', () => {
  beforeEach(() => {
    $activeSessionId.set(null)
    $gateway.set(null as any)
  })

  it('passes active session id when none provided explicitly', async () => {
    const mockRequest = vi.fn().mockResolvedValue({
      content: [{ type: 'text', text: 'ok' }]
    })
    $gateway.set({ request: mockRequest } as any)
    $activeSessionId.set('session-active-123')

    const res = await callMcpAppTool({
      appUri: 'ui://weather-server/app',
      server: 'weather-server',
      name: 'get_forecast',
      arguments: { city: 'Paris' }
    })

    expect(mockRequest).toHaveBeenCalledWith('mcp.tools.call', {
      session_id: 'session-active-123',
      server: 'weather-server',
      name: 'get_forecast',
      arguments: { city: 'Paris' }
    })
    expect(res).toEqual({
      content: [{ type: 'text', text: 'ok' }]
    })
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
