import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Owner routing: a card's calls and reads go through the gateway that owns its
// session, never the active one (the two differ once focus moves elsewhere).

const mocks = vi.hoisted(() => ({
  owner: vi.fn(),
  requests: [] as { instance: unknown; method: string; params: unknown }[]
}))

vi.mock('@/hermes', () => ({
  setApiRequestConnection: vi.fn(),
  HermesGateway: class {
    connectionState = 'closed'
    connect = async (): Promise<void> => {
      this.connectionState = 'open'
    }
    close = (): void => {
      this.connectionState = 'closed'
    }
    request = vi.fn(async (method: string, params: unknown) => {
      mocks.requests.push({ instance: this, method, params })

      return { content: [{ type: 'text', text: 'ok' }], server: 'w', uri: 'ui://w/app', contents: [] }
    })
    onEvent = vi.fn(() => () => {})
    onState = vi.fn(() => () => {})
  }
}))
vi.mock('@/store/session', () => ({ setConnection: vi.fn(), setGatewayState: vi.fn() }))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline: vi.fn() }))
vi.mock('@/app/session/hooks/use-session-actions/utils', () => ({ resolveSessionOwner: mocks.owner }))

const { HermesGateway } = await import('@/hermes')
const gateway = await import('@/store/gateway')
const { callMcpAppTool, pinMcpAppOwner, readMcpAppResource } = await import('./resolve')

const homelab = {
  authMode: 'token',
  baseUrl: 'https://homelab.invalid',
  mode: 'remote',
  profile: 'research',
  token: 'fake-test-token',
  wsUrl: 'wss://homelab.invalid/api/ws?token=fake-test-token'
}

const toolReq = { appUri: 'ui://w/app', server: 'w', name: 'get_forecast', arguments: {} }

describe('owner-pinned MCP app routing', () => {
  let active: InstanceType<typeof HermesGateway>

  beforeEach(() => {
    mocks.requests.length = 0
    gateway.configureGatewayRegistry({ onEvent: vi.fn() })
    active = new HermesGateway() as InstanceType<typeof HermesGateway>
    ;(active as unknown as { connectionState: string }).connectionState = 'open'
    gateway.setPrimaryGateway(active as never, 'default')
    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
      getConnection: vi.fn(async () => homelab),
      getConnectionFor: vi.fn(async () => homelab)
    }
    mocks.owner.mockResolvedValue({ connectionId: 'homelab', profile: 'research' })
  })

  afterEach(() => {
    gateway.closeSecondaryGateways()
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('sends the read and the tool call through the owning gateway while another one is active', async () => {
    await gateway.ensureGatewayForAgent('homelab', 'research')
    const owning = gateway.$gateway.get()
    expect(owning).not.toBe(active)
    await gateway.ensureGatewayForProfile('default')
    expect(gateway.$gateway.get()).toBe(active)

    const owner = pinMcpAppOwner('session-a')

    await readMcpAppResource('w', 'ui://w/app', owner)
    await callMcpAppTool(toolReq, 'session-a', owner)

    expect(mocks.requests.map(r => [r.method, r.instance === owning])).toEqual([
      ['mcp.resources.read', true],
      ['mcp.tools.call', true]
    ])
    expect(mocks.requests.some(r => r.instance === active)).toBe(false)
  })

  it('refuses when the owning gateway is not connected, and the active one gets nothing', async () => {
    const owner = pinMcpAppOwner('session-a')

    await expect(readMcpAppResource('w', 'ui://w/app', owner)).rejects.toThrow(/not connected/)
    await expect(callMcpAppTool(toolReq, 'session-a', owner)).rejects.toThrow(/not connected/)
    expect(mocks.requests).toEqual([])
  })

  it('refuses when no owner is known, never using the active gateway', async () => {
    mocks.owner.mockResolvedValue(undefined)
    const owner = pinMcpAppOwner('session-a')

    await expect(readMcpAppResource('w', 'ui://w/app', owner)).rejects.toThrow(/not attached/)
    await expect(callMcpAppTool(toolReq, 'session-a', owner)).rejects.toThrow(/not attached/)
    await expect(callMcpAppTool(toolReq, null, owner)).rejects.toThrow(/not attached to a session/)
    expect(mocks.requests).toEqual([])
  })
})
