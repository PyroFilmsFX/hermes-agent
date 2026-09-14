// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { requestModelOptions } from '@/lib/model-options'
import { requestGatewayForAgent } from '@/store/gateway'
import { ensureGatewayAgent, ensureGatewayProfile, resolveNewChatOwnerRoute } from '@/store/profile'
import {
  setCurrentFastMode,
  setCurrentModel,
  setCurrentModelSource,
  setCurrentProvider,
  setCurrentReasoningEffort
} from '@/store/session'

import { desktopSessionCreateParams } from './index'

vi.mock('@/lib/model-options', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestModelOptions: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn(() => Promise.resolve()),
  ensureGatewayProfile: vi.fn(() => Promise.resolve()),
  resolveNewChatOwnerRoute: vi.fn(() => null)
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  activeGateway: vi.fn(() => null),
  requestGatewayForAgent: vi.fn()
}))

const storage = new Map<string, string>()
Object.defineProperty(window, 'localStorage', {
  value: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, val: string) => {
      storage.set(key, String(val))
    },
    removeItem: (key: string) => {
      storage.delete(key)
    },
    clear: () => {
      storage.clear()
    }
  },
  writable: true
})

describe('desktopSessionCreateParams', () => {
  beforeEach(() => {
    storage.clear()
    setCurrentModelSource('default')
    setCurrentModel('')
    setCurrentProvider('')
    setCurrentReasoningEffort('')
    setCurrentFastMode(false)
    vi.mocked(resolveNewChatOwnerRoute).mockReturnValue(null)
    vi.mocked(ensureGatewayAgent).mockResolvedValue(undefined)
    vi.mocked(ensureGatewayProfile).mockResolvedValue(undefined)
    vi.mocked(requestGatewayForAgent).mockReset()
    vi.clearAllMocks()
  })

  it('omits model and provider when model source is default', async () => {
    setCurrentModelSource('default')
    setCurrentModel('claude-opus-4-6')
    setCurrentProvider('claude-agent-sdk')

    const params = await desktopSessionCreateParams('/workspace')

    expect(params).not.toHaveProperty('model')
    expect(params).not.toHaveProperty('provider')
    expect(params.source).toBe('desktop')
    expect(params.cwd).toBe('/workspace')
    expect(requestModelOptions).not.toHaveBeenCalled()
  })

  it('includes both model and provider when manual source provides both', async () => {
    setCurrentModelSource('manual')
    setCurrentModel('claude-opus-4-6')
    setCurrentProvider('claude-agent-sdk')

    const params = await desktopSessionCreateParams('/workspace')

    expect(params.model).toBe('claude-opus-4-6')
    expect(params.provider).toBe('claude-agent-sdk')
    expect(requestModelOptions).not.toHaveBeenCalled()
  })

  it('resolves provider from catalog when manual source has model without provider', async () => {
    setCurrentModelSource('manual')
    setCurrentModel('claude-opus-4-6')
    setCurrentProvider('')

    vi.mocked(requestModelOptions).mockResolvedValueOnce({
      provider: 'claude-agent-sdk',
      providers: [
        {
          slug: 'claude-agent-sdk',
          name: 'Claude Agent SDK',
          models: ['claude-opus-4-6', 'claude-fable-5-1']
        },
        {
          slug: 'anthropic',
          name: 'Anthropic',
          models: ['claude-opus-4-6']
        }
      ]
    })

    const params = await desktopSessionCreateParams('/workspace')

    expect(requestModelOptions).toHaveBeenCalled()
    expect(params.model).toBe('claude-opus-4-6')
    expect(params.provider).toBe('claude-agent-sdk')
  })

  it('omits both model and provider when manual model cannot be resolved from catalog', async () => {
    setCurrentModelSource('manual')
    setCurrentModel('unknown-model')
    setCurrentProvider('')

    vi.mocked(requestModelOptions).mockResolvedValueOnce({
      provider: 'claude-agent-sdk',
      providers: [
        {
          slug: 'claude-agent-sdk',
          name: 'Claude Agent SDK',
          models: ['claude-fable-5-1']
        }
      ]
    })

    const params = await desktopSessionCreateParams('/workspace')

    expect(requestModelOptions).toHaveBeenCalled()
    expect(params).not.toHaveProperty('model')
    expect(params).not.toHaveProperty('provider')
  })

  it('resolves a missing provider from the target backend catalog', async () => {
    setCurrentModelSource('manual')
    setCurrentModel('shared-model')
    setCurrentProvider('')

    const route = { connectionId: 'backend-b', profile: 'default', targetProfile: 'target-profile' }
    vi.mocked(resolveNewChatOwnerRoute).mockReturnValue(route)
    vi.mocked(requestGatewayForAgent).mockResolvedValueOnce({
      provider: 'target-provider',
      providers: [
        { slug: 'target-provider', name: 'Target Provider', models: ['shared-model'] },
        { slug: 'ambient-provider', name: 'Ambient Provider', models: ['shared-model'] }
      ]
    } as never)
    vi.mocked(requestModelOptions).mockImplementationOnce(async options => {
      return options.request
        ? options.request('model.options', { explicit_only: true, profile: options.profile })
        : Promise.reject(new Error('missing target request'))
    })

    const params = await desktopSessionCreateParams('/workspace', route)

    expect(ensureGatewayAgent).toHaveBeenCalledWith('backend-b', 'default')
    expect(requestGatewayForAgent).toHaveBeenCalledWith('backend-b', 'default', 'model.options', {
      explicit_only: true,
      profile: 'target-profile'
    })
    expect(params).toMatchObject({ model: 'shared-model', provider: 'target-provider' })
  })

  it('keeps the selector snapshot taken before a deferred catalog lookup', async () => {
    setCurrentModelSource('manual')
    setCurrentModel('before-model')
    setCurrentProvider('')
    setCurrentReasoningEffort('high')
    setCurrentFastMode(true)

    let resolveCatalog!: (value: unknown) => void
    vi.mocked(requestModelOptions).mockReturnValueOnce(
      new Promise(resolve => {
        resolveCatalog = resolve
      }) as never
    )

    const paramsPromise = desktopSessionCreateParams('/workspace')
    await Promise.resolve()

    setCurrentModelSource('manual')
    setCurrentModel('after-model')
    setCurrentProvider('after-provider')
    setCurrentReasoningEffort('low')
    setCurrentFastMode(false)
    resolveCatalog({
      provider: 'before-provider',
      providers: [{ slug: 'before-provider', name: 'Before Provider', models: ['before-model'] }]
    })

    await expect(paramsPromise).resolves.toMatchObject({
      model: 'before-model',
      provider: 'before-provider',
      reasoning_effort: 'high',
      fast: true
    })
  })
})
