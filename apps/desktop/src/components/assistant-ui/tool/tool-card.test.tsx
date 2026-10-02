// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createPluginContext } from '@/contrib/plugin'
import { registry } from '@/contrib/registry'
import { TOOL_CARD_AREA, type ToolCardProps } from '@/lib/tool-cards'

import { fixtureToolCardPlugin } from './fixtures/test-tool-card-plugin'

vi.mock('@assistant-ui/react', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useAuiState: (select: (state: unknown) => unknown) =>
    select({ message: { id: 'msg-1', status: { type: 'complete' } }, thread: { isRunning: false } })
}))

const { ToolFallback } = await import('./fallback')

const cleanups: (() => void)[] = []

afterEach(() => {
  cleanup()
  while (cleanups.length > 0) {
    cleanups.pop()?.()
  }
})

function renderToolRow(overrides: Record<string, unknown> = {}) {
  const props = {
    args: {},
    result: { value: 'default-result' },
    toolCallId: 'call-1',
    toolName: 'mcp__hermes-mcp-2026-fixture__structured_valid',
    ...overrides
  } as unknown as ComponentProps<typeof ToolFallback>

  return render(<ToolFallback {...props} />)
}

describe('MCP tool.card contribution area', () => {
  it('bundled test plugin registers a tool.card renderer that receives structuredContent and wins', () => {
    const ctx = createPluginContext(fixtureToolCardPlugin.id, d => cleanups.push(d))
    fixtureToolCardPlugin.register(ctx)

    renderToolRow({
      toolName: 'mcp__hermes-mcp-2026-fixture__structured_valid',
      structuredContent: { value: 'custom structured payload' }
    })

    const card = screen.getByTestId('mcp-structured-tool-card')
    expect(card).toBeTruthy()
    expect(screen.getByTestId('mcp-structured-value').textContent).toBe('custom structured payload')
    expect(card.getAttribute('data-tool')).toBe('mcp__hermes-mcp-2026-fixture__structured_valid')
  })

  it('unregistered tool falls back to the default tool card unchanged', () => {
    const ctx = createPluginContext(fixtureToolCardPlugin.id, d => cleanups.push(d))
    fixtureToolCardPlugin.register(ctx)

    const { container } = renderToolRow({
      toolName: 'mcp__hermes-mcp-2026-fixture__unregistered_tool',
      structuredContent: { value: 'some content' }
    })

    expect(screen.queryByTestId('mcp-structured-tool-card')).toBeNull()
    expect(container.querySelector('[data-slot="tool-block"]')).toBeTruthy()
  })

  it('lookup by outputSchema $id works when registered by schema ID', () => {
    const schemaId = 'https://example.com/schemas/temperature-card.json'

    const dispose = registry.register({
      id: 'temp-card-plugin:temp-card',
      area: TOOL_CARD_AREA,
      data: {
        schemaId,
        render: ({ structuredContent }: ToolCardProps<{ temp: number }>) => (
          <div data-testid="schema-id-card">
            <span>Temperature: {structuredContent.temp}°C</span>
          </div>
        )
      }
    })
    cleanups.push(dispose)

    renderToolRow({
      toolName: 'mcp__weather__get_temp',
      outputSchema: {
        $id: schemaId,
        type: 'object',
        properties: { temp: { type: 'number' } }
      },
      structuredContent: { temp: 22 }
    })

    expect(screen.getByTestId('schema-id-card')).toBeTruthy()
    expect(screen.getByText('Temperature: 22°C')).toBeTruthy()
  })

  it('a renderer that throws falls back to the default card via ErrorBoundary', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    const dispose = registry.register({
      id: 'broken-card-plugin:broken-card',
      area: TOOL_CARD_AREA,
      data: {
        tool: 'hermes-mcp-2026-fixture/structured_valid',
        render: () => {
          throw new Error('Renderer crashed intentionally')
        }
      }
    })
    cleanups.push(dispose)

    const { container } = renderToolRow({
      toolName: 'mcp__hermes-mcp-2026-fixture__structured_valid',
      structuredContent: { value: 'will-crash' }
    })

    // The thrown error should have triggered ErrorBoundary fallback to ToolEntry
    expect(container.querySelector('[data-slot="tool-block"]')).toBeTruthy()
    expect(screen.queryByTestId('mcp-structured-tool-card')).toBeNull()

    consoleError.mockRestore()
  })
})
