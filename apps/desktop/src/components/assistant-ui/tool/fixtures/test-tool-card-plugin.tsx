import type { HermesPlugin, PluginContext } from '@/contrib/plugin'
import { TOOL_CARD_AREA, type ToolCardProps } from '@/lib/tool-cards'

export interface FixtureEchoOutput {
  value: string
}

export const FIXTURE_TOOL_CARD_PLUGIN_ID = 'test-mcp-fixture-tool-card'

/**
 * Bundled test plugin (test-only) that registers a `tool.card` contribution
 * for the fixture server's structured tool (`hermes-mcp-2026-fixture/structured_valid`).
 */
export const fixtureToolCardPlugin: HermesPlugin = {
  id: FIXTURE_TOOL_CARD_PLUGIN_ID,
  name: 'Test MCP Fixture Tool Card Plugin',
  description: 'Test plugin providing tool.card renderer for hermes-mcp-2026-fixture/structured_valid',
  defaultEnabled: true,
  register: (ctx: PluginContext) => {
    ctx.register({
      area: TOOL_CARD_AREA,
      id: 'fixture-structured-valid-card',
      data: {
        tool: 'hermes-mcp-2026-fixture/structured_valid',
        render: ({ structuredContent, toolName }: ToolCardProps<FixtureEchoOutput>) => (
          <div data-testid="mcp-structured-tool-card" data-tool={toolName}>
            <span data-testid="mcp-structured-value">{structuredContent?.value}</span>
          </div>
        )
      }
    })
  }
}

export default fixtureToolCardPlugin
