import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'

import { shouldBlockFrameNavigation } from '../../../electron/frame-navigation'
import { callMcpAppTool } from './resolve'

describe('frame navigation', () => {
  it('allows main frame navigation', () => {
    expect(shouldBlockFrameNavigation(true, 'https://example.com')).toBe(false)
    expect(shouldBlockFrameNavigation(true, 'http://example.com')).toBe(false)
    expect(shouldBlockFrameNavigation(true, 'file:///path/to/file')).toBe(false)
  })

  it('allows about:srcdoc in sub-frames', () => {
    expect(shouldBlockFrameNavigation(false, 'about:srcdoc')).toBe(false)
    expect(shouldBlockFrameNavigation(false, 'about:SrcDoc')).toBe(false)
  })

  it('allows about:blank in sub-frames', () => {
    expect(shouldBlockFrameNavigation(false, 'about:blank')).toBe(false)
    expect(shouldBlockFrameNavigation(false, 'about:Blank')).toBe(false)
  })

  it('blocks http in sub-frames', () => {
    expect(shouldBlockFrameNavigation(false, 'http://example.com')).toBe(true)
  })

  it('blocks https in sub-frames', () => {
    expect(shouldBlockFrameNavigation(false, 'https://example.com')).toBe(true)
  })

  it('blocks other protocols in sub-frames', () => {
    expect(shouldBlockFrameNavigation(false, 'file:///etc/passwd')).toBe(true)
    expect(shouldBlockFrameNavigation(false, 'javascript:alert(1)')).toBe(true)
  })
})

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
