import type { ReactNode } from 'react'

import type { ToolPart } from '@/components/assistant-ui/tool/fallback-model/types'
import type { Contribution } from '@/contrib/types'

export const TOOL_CARD_AREA = 'tool.card'

/**
 * Props handed to a `tool.card` contribution's `render`.
 */
export interface ToolCardProps<T = unknown> {
  /** The validated structuredContent payload returned by the MCP tool. */
  structuredContent: T
  /** The full registered tool name (e.g. `mcp__server__tool` or `server/tool`). */
  toolName: string
  /** The call id for this tool invocation. */
  toolCallId?: string
  /** Arguments passed into the tool call. */
  args?: unknown
  /** The raw result object or string. */
  result?: unknown
  /** Whether the tool failed or raised an error. */
  isError?: boolean
  /** Timestamp when completed. */
  completedAt?: number
  /** Timestamp when started. */
  timestamp?: number
  /** The complete ToolPart representing this call. */
  part: ToolPart
}

/**
 * Payload (`data`) of a `tool.card` contribution.
 */
export interface ToolCardContribution {
  /** Target tool identifier in `server/tool` format (e.g. `'hermes-mcp-2026-fixture/structured_valid'`),
   *  or the tool's `outputSchema` `$id`. */
  key?: string
  /** Explicit server/tool format (e.g. `'hermes-mcp-2026-fixture/structured_valid'`). */
  tool?: string
  /** Explicit outputSchema `$id` or `id`. */
  schemaId?: string
  /** Alternative alias for key/tool. */
  target?: string
  /** Renders the custom card. Mounted inside an ErrorBoundary that falls back to the default card on throw. */
  render: (props: ToolCardProps) => ReactNode
}

/**
 * Candidate lookup keys for a tool invocation:
 * - Direct toolName (e.g. 'mcp__fixture__structured_valid')
 * - 'server/tool' extracted from 'mcp__server__tool' (e.g. 'fixture/structured_valid')
 * - 'server/tool' direct if toolName has a slash
 * - outputSchema $id or id (e.g. 'https://example.com/schemas/tool.json')
 */
export function candidateToolCardKeys(
  toolName: string,
  outputSchema?: Record<string, unknown> | null
): string[] {
  const keys = new Set<string>()
  if (!toolName) return []

  keys.add(toolName)

  if (toolName.includes('/')) {
    keys.add(toolName)
    const [server, ...rest] = toolName.split('/')
    keys.add(`mcp__${server}__${rest.join('/')}`)
  }

  if (toolName.startsWith('mcp__')) {
    const withoutPrefix = toolName.slice(5)
    const sepIdx = withoutPrefix.indexOf('__')
    if (sepIdx !== -1) {
      const server = withoutPrefix.slice(0, sepIdx)
      const tool = withoutPrefix.slice(sepIdx + 2)
      keys.add(`${server}/${tool}`)
    }
  }

  if (outputSchema && typeof outputSchema === 'object') {
    const id = outputSchema.$id ?? outputSchema.id
    if (typeof id === 'string' && id) {
      keys.add(id)
    }
  }

  return Array.from(keys)
}

/**
 * Find the first contribution in `tool.card` that claims one of the candidate keys.
 */
export function findToolCardContribution(
  contributions: readonly Contribution[],
  toolName: string,
  outputSchema?: Record<string, unknown> | null
): Contribution | undefined {
  const candidates = candidateToolCardKeys(toolName, outputSchema)
  if (candidates.length === 0) return undefined

  return contributions.find(c => {
    const data = (c.data || {}) as ToolCardContribution
    const contributionKeys: string[] = []

    if (data.key) contributionKeys.push(data.key)
    if (data.tool) contributionKeys.push(data.tool)
    if (data.schemaId) contributionKeys.push(data.schemaId)
    if (data.target) contributionKeys.push(data.target)

    if (c.id) {
      contributionKeys.push(c.id)
      const colonIdx = c.id.indexOf(':')
      if (colonIdx !== -1) {
        contributionKeys.push(c.id.slice(colonIdx + 1))
      }
    }

    return contributionKeys.some(k => candidates.includes(k))
  })
}

/**
 * Extract structuredContent from a ToolPart across wire representations:
 * - part.structuredContent (canonical)
 * - part.result.structuredContent
 * - part.result itself (structuredContent-only servers where result is the object)
 */
export function extractStructuredContent(part: ToolPart): unknown {
  if (part.structuredContent !== undefined) {
    return part.structuredContent
  }
  if (part.result && typeof part.result === 'object') {
    const rec = part.result as Record<string, unknown>
    if ('structuredContent' in rec && rec.structuredContent !== undefined) {
      return rec.structuredContent
    }
    return part.result
  }
  return undefined
}
