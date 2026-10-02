import { describe, expect, it, vi, beforeEach } from 'vitest'

import { $composerDraft, setComposerDraft } from '@/store/composer'
import { $gateway } from '@/store/gateway'
import {
  executeMcpPrompt,
  getMcpPrompt,
  invalidateMcpPromptIndex,
  listMcpPrompts,
  parsePromptArgs
} from './mcp-prompts'

describe('parsePromptArgs', () => {
  const declared = [
    { name: 'name', required: true, description: 'User name' },
    { name: 'tone', required: false, description: 'Tone of voice' }
  ]

  it('maps positional arguments when declaredArgs are provided', () => {
    expect(parsePromptArgs('Alice', declared)).toEqual({ name: 'Alice' })
    expect(parsePromptArgs('Alice formal', declared)).toEqual({ name: 'Alice', tone: 'formal' })
  })

  it('parses key=value and quoted arguments', () => {
    expect(parsePromptArgs('name=Alice tone="very friendly"', declared)).toEqual({
      name: 'Alice',
      tone: 'very friendly'
    })
  })

  it('parses --flag style arguments', () => {
    expect(parsePromptArgs('--name Alice --tone casual', declared)).toEqual({
      name: 'Alice',
      tone: 'casual'
    })
    expect(parsePromptArgs('--name="Bob Ross" --tone=calm', declared)).toEqual({
      name: 'Bob Ross',
      tone: 'calm'
    })
  })

  it('returns empty record on empty or missing input', () => {
    expect(parsePromptArgs('')).toEqual({})
    expect(parsePromptArgs('   ')).toEqual({})
  })
})

describe('listMcpPrompts and getMcpPrompt', () => {
  beforeEach(() => {
    invalidateMcpPromptIndex()
    setComposerDraft('')
  })

  it('fetches prompt list via mcp.prompts.list and caches results', async () => {
    const mockRequest = vi.fn().mockResolvedValue({
      prompts: [
        {
          server: 'mcp2026',
          name: 'greeting',
          description: 'A greeting prompt with a name argument.',
          arguments: [{ name: 'name', required: true }],
          command: 'mcp2026:greeting'
        }
      ]
    })

    $gateway.set({ request: mockRequest } as any)

    const prompts1 = await listMcpPrompts()
    expect(prompts1).toHaveLength(1)
    expect(prompts1[0].name).toBe('greeting')
    expect(prompts1[0].command).toBe('mcp2026:greeting')
    expect(mockRequest).toHaveBeenCalledTimes(1)
    expect(mockRequest).toHaveBeenCalledWith('mcp.prompts.list', {})

    // Second call served from cache
    const prompts2 = await listMcpPrompts()
    expect(prompts2).toEqual(prompts1)
    expect(mockRequest).toHaveBeenCalledTimes(1)

    // After invalidation, requests again
    invalidateMcpPromptIndex()
    await listMcpPrompts()
    expect(mockRequest).toHaveBeenCalledTimes(2)
  })

  it('fetches rendered prompt via mcp.prompts.get', async () => {
    const mockRequest = vi.fn().mockResolvedValue({
      description: 'A greeting prompt',
      messages: [{ role: 'user', content: 'Hello, Justin!' }],
      text: 'Hello, Justin!'
    })

    $gateway.set({ request: mockRequest } as any)

    const result = await getMcpPrompt('mcp2026', 'greeting', { name: 'Justin' })
    expect(mockRequest).toHaveBeenCalledWith('mcp.prompts.get', {
      server: 'mcp2026',
      name: 'greeting',
      arguments: { name: 'Justin' }
    })
    expect(result.text).toBe('Hello, Justin!')
    expect(result.messages[0].content).toBe('Hello, Justin!')
  })

  it('executes prompt command, rendering and inserting into composer draft without auto-sending', async () => {
    const mockRequest = vi.fn().mockImplementation((method: string, params: any) => {
      if (method === 'mcp.prompts.list') {
        return Promise.resolve({
          prompts: [
            {
              server: 'mcp2026',
              name: 'greeting',
              arguments: [{ name: 'name', required: true }],
              command: 'mcp2026:greeting'
            }
          ]
        })
      }
      if (method === 'mcp.prompts.get') {
        return Promise.resolve({
          messages: [{ role: 'user', content: `Hello, ${params.arguments?.name}!` }],
          text: `Hello, ${params.arguments?.name}!`
        })
      }
      return Promise.reject(new Error(`Unknown method: ${method}`))
    })

    $gateway.set({ request: mockRequest } as any)

    // Before execution draft is empty
    expect($composerDraft.get()).toBe('')

    const rendered = await executeMcpPrompt('/mcp2026:greeting name=Antigravity')

    expect(rendered).toBe('Hello, Antigravity!')
    // Crucial acceptance: rendered messages inserted into composer draft for owner to review and send
    expect($composerDraft.get()).toBe('Hello, Antigravity!')
    // Verify prompt.submit was NEVER called
    expect(mockRequest).not.toHaveBeenCalledWith('prompt.submit', expect.anything())
  })
})
