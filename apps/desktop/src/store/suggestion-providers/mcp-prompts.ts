import { requestComposerFocus } from '@/app/chat/composer/focus'
import { registerDraftProvider } from '@/store/composer-suggestions'
import { setComposerDraft } from '@/store/composer'
import { $gateway } from '@/store/gateway'

/**
 * MCP Prompts suggestion provider & client.
 *
 * Discovers prompt templates advertised by connected MCP servers via
 * `mcp.prompts.list` and renders them with `mcp.prompts.get`.
 *
 * When selected or executed, the rendered prompt text is inserted into the
 * composer draft for the owner to inspect and send. It is NEVER auto-sent or
 * submitted on the user's behalf.
 */

export interface McpPromptArgument {
  name: string
  description?: string
  required?: boolean
}

export interface McpPromptInfo {
  server: string
  name: string
  description?: string
  arguments?: McpPromptArgument[]
  command: string
}

export interface McpPromptMessage {
  role: string
  content: string
}

export interface McpPromptsGetResult {
  description?: string
  messages: McpPromptMessage[]
  text: string
}

const PROMPTS_TTL_MS = 5 * 60_000

let promptCache: McpPromptInfo[] | null = null
let promptCacheAt = 0

/** Drop the cached MCP prompt index (e.g. after server reload). */
export function invalidateMcpPromptIndex(): void {
  promptCache = null
  promptCacheAt = 0
}

/**
 * Parse prompt command arguments from input string, supporting:
 * - key=value, key="quoted value"
 * - --key=value, --key value
 * - positional arguments mapped to declared arguments
 */
export function parsePromptArgs(
  argStr: string,
  declaredArgs?: McpPromptArgument[]
): Record<string, string> {
  const result: Record<string, string> = {}
  if (!argStr || !argStr.trim()) {
    return result
  }

  const tokenRegex =
    /([a-zA-Z0-9_.-]+)=(?:"([^"]*)"|'([^']*)')|--([a-zA-Z0-9_.-]+)=(?:"([^"]*)"|'([^']*)')|"([^"]*)"|'([^']*)'|[^\s]+/g
  let match: RegExpExecArray | null
  const tokens: Array<{ key?: string; value: string }> = []

  while ((match = tokenRegex.exec(argStr.trim())) !== null) {
    if (match[1] !== undefined) {
      tokens.push({ key: match[1], value: match[2] ?? match[3] ?? '' })
    } else if (match[4] !== undefined) {
      tokens.push({ key: match[4], value: match[5] ?? match[6] ?? '' })
    } else if (match[7] !== undefined) {
      tokens.push({ value: match[7] })
    } else if (match[8] !== undefined) {
      tokens.push({ value: match[8] })
    } else {
      const raw = match[0]
      if (raw.startsWith('--')) {
        const stripped = raw.slice(2)
        if (stripped.includes('=')) {
          const [k, ...rest] = stripped.split('=')
          tokens.push({ key: k, value: rest.join('=') })
        } else {
          tokens.push({ key: stripped, value: '' })
        }
      } else if (raw.includes('=')) {
        const [k, ...rest] = raw.split('=')
        tokens.push({ key: k, value: rest.join('=') })
      } else {
        tokens.push({ value: raw })
      }
    }
  }

  let positionalIdx = 0
  for (let i = 0; i < tokens.length; i++) {
    const item = tokens[i]
    if (item.key !== undefined) {
      const cleanKey = item.key.replace(/^--/, '')
      if (item.value === '' && i + 1 < tokens.length && tokens[i + 1].key === undefined) {
        result[cleanKey] = tokens[i + 1].value
        i++
      } else {
        result[cleanKey] = item.value || 'true'
      }
    } else {
      if (declaredArgs && positionalIdx < declaredArgs.length) {
        result[declaredArgs[positionalIdx].name] = item.value
        positionalIdx++
      }
    }
  }

  return result
}

/**
 * List prompt templates from connected MCP servers via gateway RPC `mcp.prompts.list`.
 */
export async function listMcpPrompts(server?: string): Promise<McpPromptInfo[]> {
  if (promptCache && Date.now() - promptCacheAt < PROMPTS_TTL_MS) {
    if (server) {
      return promptCache.filter(p => p.server === server)
    }
    return promptCache
  }

  const gateway = $gateway.get()
  if (!gateway) {
    return []
  }

  try {
    const res = await gateway.request<{ prompts: McpPromptInfo[] }>('mcp.prompts.list', server ? { server } : {})
    const prompts = res?.prompts ?? []
    if (!server) {
      promptCache = prompts
      promptCacheAt = Date.now()
    }
    return prompts
  } catch {
    return []
  }
}

/**
 * Get and render an MCP prompt template via gateway RPC `mcp.prompts.get`.
 */
export async function getMcpPrompt(
  server: string,
  name: string,
  args?: Record<string, string>
): Promise<McpPromptsGetResult> {
  const gateway = $gateway.get()
  if (!gateway) {
    throw new Error('Gateway not connected')
  }

  return gateway.request<McpPromptsGetResult>('mcp.prompts.get', {
    server,
    name,
    arguments: args ?? {}
  })
}

/**
 * Insert rendered prompt text into the composer draft.
 * Crucial invariant: never auto-sends; sets composer draft for the owner to review.
 */
export function insertMcpPromptDraft(renderedText: string): void {
  setComposerDraft(renderedText)
  requestComposerFocus()
}

/**
 * Parse and execute a prompt slash command string `/<server>:<prompt> [args]`
 * and insert the rendered messages into the composer draft.
 */
export async function executeMcpPrompt(commandStr: string): Promise<string> {
  let cleaned = commandStr.trim()
  if (cleaned.startsWith('/')) {
    cleaned = cleaned.slice(1)
  }

  const spaceIdx = cleaned.indexOf(' ')
  const cmdPart = spaceIdx === -1 ? cleaned : cleaned.slice(0, spaceIdx)
  const argPart = spaceIdx === -1 ? '' : cleaned.slice(spaceIdx + 1).trim()

  const colonIdx = cmdPart.indexOf(':')
  if (colonIdx === -1) {
    throw new Error(`Invalid MCP prompt command format: ${commandStr}`)
  }

  const server = cmdPart.slice(0, colonIdx)
  const promptName = cmdPart.slice(colonIdx + 1)

  const prompts = await listMcpPrompts(server)
  const promptDef = prompts.find(p => p.server === server && p.name === promptName)
  const parsedArgs = parsePromptArgs(argPart, promptDef?.arguments)

  const result = await getMcpPrompt(server, promptName, parsedArgs)
  const renderedText = result.text || result.messages?.map(m => m.content).join('\n') || ''

  insertMcpPromptDraft(renderedText)
  return renderedText
}

/**
 * Draft provider: when the composer draft mentions an MCP prompt `/<server>:<prompt>`,
 * offer a suggestion pill to render and insert the prompt into the draft.
 */
registerDraftProvider('mcp-prompt', async ({ text }) => {
  const trimmed = text.trim()
  if (!trimmed.startsWith('/') || !trimmed.includes(':')) {
    return []
  }

  // Match /<server>:<prompt> pattern at start of draft
  const match = /^\/([a-zA-Z0-9_-]+):([a-zA-Z0-9_-]+)(?:\s+(.*))?$/.exec(trimmed)
  if (!match) {
    return []
  }

  const [, server, promptName, argStr = ''] = match
  const promptKey = `${server}:${promptName}`

  return [
    {
      id: promptKey,
      provider: 'mcp-prompt',
      label: `Insert prompt: ${promptKey}`,
      tip: `Render and insert /${promptKey} into composer draft`,
      workingLabel: `Rendering /${promptKey}…`,
      workingTip: `Rendering prompt template from ${server}…`,
      doneLabel: `Prompt inserted`,
      doneTip: `Rendered /${promptKey} messages inserted into composer`,
      icon: 'sparkle',
      invoke: async () => {
        await executeMcpPrompt(trimmed)
      }
    }
  ]
})
