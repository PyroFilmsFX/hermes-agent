import { COMPOSER_AREAS, type ComposerAtCompletionItem } from '@/app/chat/composer/contrib'
import { requestComposerFocus, requestComposerInsertRefs } from '@/app/chat/composer/focus'
import { registry } from '@/contrib/registry'
import { type ComposerSuggestion, registerDraftProvider } from '@/store/composer-suggestions'
import { $gateway } from '@/store/gateway'

/**
 * MCP resource suggestion provider & client (MCP 2026 unit M6).
 *
 * Discovers resources advertised by connected MCP servers via `mcp.resources.list`.
 * Offers resources:
 * 1. Inside the composer `@` completion popover via `COMPOSER_AREAS.atCompletions`.
 * 2. As draft suggestion pills when the draft text mentions a resource keyword.
 *
 * When selected, inserts a reference chip into the composer as `@resource:<server>:<uri>` (the server-qualified
 * form the backend parses; a bare URI is ambiguous across servers).
 */

export interface McpResourceItem {
  server: string
  uri: string
  name: string
  description?: string | null
  mimeType?: string | null
}

export interface SuggestibleResource {
  server: string
  uri: string
  name: string
  description?: string | null
  keywords: string[]
}

export interface McpResourceMatch {
  server: string
  uri: string
  name: string
  keyword: string
}

const RESOURCES_TTL_MS = 5 * 60_000
const MAX_MATCHES = 2

let rawResourcesCache: McpResourceItem[] | null = null
let cachedResources: SuggestibleResource[] | null = null
let cachedAt = 0

/** Invalidate cached resource suggestion index. */
export function invalidateMcpResourceSuggestionIndex(): void {
  rawResourcesCache = null
  cachedResources = null
  cachedAt = 0
}

const escapeRegex = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** Build index from resource items, extracting keywords for matching. */
export function buildMcpResourceSuggestionIndex(
  resources: readonly McpResourceItem[]
): SuggestibleResource[] {
  return resources.map(resource => {
    const rawKeywords = new Set<string>()

    const name = resource.name || resource.uri
    rawKeywords.add(name.toLowerCase())

    // Split name words
    for (const part of name.toLowerCase().split(/[\s/_-]+/)) {
      if (part.length >= 2) {
        rawKeywords.add(part)
      }
    }

    // URI scheme and path segments
    const uriClean = resource.uri.toLowerCase()
    rawKeywords.add(uriClean)
    const withoutScheme = uriClean.replace(/^[a-z0-9+.-]+:\/\//, '')
    for (const seg of withoutScheme.split(/[\s/_-]+/)) {
      if (seg.length >= 2) {
        rawKeywords.add(seg)
      }
    }

    return {
      server: resource.server,
      uri: resource.uri,
      name,
      description: resource.description,
      keywords: Array.from(rawKeywords)
    }
  })
}

/**
 * List resources from connected MCP servers via gateway RPC `mcp.resources.list`.
 */
export async function listMcpResources(server?: string): Promise<McpResourceItem[]> {
  if (rawResourcesCache && Date.now() - cachedAt < RESOURCES_TTL_MS) {
    if (server) {
      return rawResourcesCache.filter(r => r.server === server)
    }
    return rawResourcesCache
  }

  const gateway = $gateway.get()
  if (!gateway) {
    return []
  }

  try {
    const res = await gateway.request<{ resources: McpResourceItem[] }>('mcp.resources.list', server ? { server } : {})
    const resources = res?.resources ?? []
    if (!server) {
      rawResourcesCache = resources
      cachedResources = buildMcpResourceSuggestionIndex(resources)
      cachedAt = Date.now()
    }
    return resources
  } catch {
    return rawResourcesCache || []
  }
}

export async function loadSuggestibleResources(): Promise<SuggestibleResource[]> {
  if (cachedResources && Date.now() - cachedAt < RESOURCES_TTL_MS) {
    return cachedResources
  }

  try {
    const resources = await listMcpResources()
    cachedResources = buildMcpResourceSuggestionIndex(resources || [])
    cachedAt = Date.now()
    return cachedResources
  } catch {
    return cachedResources || []
  }
}

/** Check if draft text already references this resource. */
export function draftContainsResource(text: string, uri: string, server?: string): boolean {
  const lower = text.toLowerCase()
  const uriLower = (server ? server + ':' + uri : uri).toLowerCase()
  return (
    lower.includes(`@resource:${uriLower}`) ||
    lower.includes(`@resource:"${uriLower}"`) ||
    lower.includes(`@resource:'${uriLower}'`) ||
    lower.includes(`@resource:\`${uriLower}\``)
  )
}

/** Whole-word keyword hit completed (or followed by space/punctuation). */
const keywordHit = (haystack: string, candidate: string): boolean => {
  const pattern = new RegExp(
    `(?<![\\p{L}\\p{N}])${escapeRegex(candidate)}(?![\\p{L}\\p{N}])`,
    'gu'
  )

  for (const match of haystack.matchAll(pattern)) {
    if (match.index + match[0].length <= haystack.length) {
      if (match.index + match[0].length === haystack.length) {
        if (candidate.includes('://') || candidate.startsWith('@')) {
          return true
        }
        return false
      }
      return true
    }
  }

  return false
}

/** Pure matcher exported for tests. */
export function matchResourceSuggestions(
  text: string,
  index: readonly SuggestibleResource[]
): McpResourceMatch[] {
  const haystack = text.toLowerCase()
  const matches: McpResourceMatch[] = []

  for (const entry of index) {
    // Already referenced in draft -> skip
    if (draftContainsResource(text, entry.uri, entry.server)) {
      continue
    }

    // Direct URI match in draft
    if (haystack.includes(entry.uri.toLowerCase())) {
      matches.push({
        server: entry.server,
        uri: entry.uri,
        name: entry.name,
        keyword: entry.uri
      })
      if (matches.length >= MAX_MATCHES) {
        break
      }
      continue
    }

    // Keyword match
    const hit = entry.keywords.find(candidate => keywordHit(haystack, candidate))
    if (hit) {
      matches.push({
        server: entry.server,
        uri: entry.uri,
        name: entry.name,
        keyword: hit
      })
      if (matches.length >= MAX_MATCHES) {
        break
      }
    }
  }

  return matches
}

/** Insert a resource reference into composer and refocus. */
export function insertResourceReference(server: string, uri: string, name?: string): void {
  requestComposerInsertRefs([{ kind: 'resource', value: server + ':' + uri, label: name || uri }])
  requestComposerFocus()
}

export function toSuggestion(match: McpResourceMatch): ComposerSuggestion {
  return {
    id: match.uri,
    provider: 'mcp-resource',
    brand: match.server,
    icon: 'package',
    label: `Attach ${match.name}`,
    tip: `Insert reference to ${match.uri}`,
    workingLabel: `Attaching ${match.name}…`,
    workingTip: 'Inserting reference',
    doneLabel: `Attached ${match.name}`,
    doneTip: `Inserted @resource:${match.server}:${match.uri}`,
    invoke: async () => {
      insertResourceReference(match.server, match.uri, match.name)
    }
  }
}

/**
 * Draft provider: when typing in composer, suggest attaching relevant resources.
 */
registerDraftProvider('mcp-resource', async ({ text }) => {
  if (!text || text.trim().length === 0) {
    return []
  }

  const index = await loadSuggestibleResources()
  if (index.length === 0) {
    return []
  }

  const matches = matchResourceSuggestions(text, index)
  return matches.map(toSuggestion)
})

/**
 * At-completion contribution: typing `@` lists resources from connected servers
 * alongside existing @-kinds.
 */
registry.register({
  id: 'mcp-resources',
  area: COMPOSER_AREAS.atCompletions,
  data: {
    provide: (query: string): ComposerAtCompletionItem[] => {
      if (!cachedResources || cachedResources.length === 0) {
        void loadSuggestibleResources()
        return []
      }

      let clean = (query || '').toLowerCase().trim()
      if (clean.startsWith('resource:')) {
        clean = clean.slice('resource:'.length).trim()
      }

      const matches = clean
        ? cachedResources.filter(
            r =>
              r.name.toLowerCase().includes(clean) ||
              r.uri.toLowerCase().includes(clean) ||
              r.server.toLowerCase().includes(clean) ||
              r.keywords.some(k => k.includes(clean))
          )
        : cachedResources

      return matches.slice(0, 20).map(r => ({
        insert: `@resource:${r.server}:${r.uri}`,
        display: r.name || r.uri,
        meta: r.server ? `${r.server} · ${r.uri}` : r.uri,
        icon: 'package'
      }))
    }
  }
})
