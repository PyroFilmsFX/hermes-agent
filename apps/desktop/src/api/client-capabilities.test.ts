import { afterEach, describe, expect, it, vi } from 'vitest'

import { HermesGateway } from './client'

class FakeSocket {
  static instances: FakeSocket[] = []
  static readonly CLOSED = 3
  static readonly OPEN = 1

  readonly sent: string[] = []
  readyState = FakeSocket.OPEN
  private listeners = new Map<string, ((event: unknown) => void)[]>()

  constructor() {
    FakeSocket.instances.push(this)
  }

  addEventListener(type: string, callback: (event: unknown) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), callback])
  }

  removeEventListener(): void {}

  close(): void {
    this.readyState = FakeSocket.CLOSED
  }

  emit(type: string, event: unknown = {}): void {
    for (const callback of this.listeners.get(type) ?? []) {
      callback(event)
    }
  }

  send(payload: string): void {
    this.sent.push(payload)
  }
}

describe('HermesGateway client capabilities', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    FakeSocket.instances = []
  })

  it('advertises mcp_elicitation alongside server_requests on gateway.ready', async () => {
    vi.stubGlobal('WebSocket', FakeSocket)
    const gateway = new HermesGateway()
    const connected = gateway.connect('ws://gateway.test/api/ws')
    const socket = FakeSocket.instances[0]!

    socket.emit('open')
    await connected
    socket.emit('message', {
      data: JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: {} } })
    })

    const frames = socket.sent.map(text => JSON.parse(text) as { method: string; params: unknown })
    const advert = frames.find(frame => frame.method === 'client.capabilities')

    expect(advert?.params).toEqual({ mcp_elicitation: true, server_requests: true })
  })
})
