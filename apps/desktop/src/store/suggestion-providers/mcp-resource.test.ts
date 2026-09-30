import { beforeEach, describe, expect, it, vi } from 'vitest'

import { COMPOSER_AREAS, type ComposerAtCompletionSource } from '@/app/chat/composer/contrib'
import * as composerFocus from '@/app/chat/composer/focus'
import { registry } from '@/contrib/registry'
import { $gateway } from '@/store/gateway'
import {
  buildMcpResourceSuggestionIndex,
  draftContainsResource,
  insertResourceReference,
  invalidateMcpResourceSuggestionIndex,
  listMcpResources,
  matchResourceSuggestions,
  type McpResourceItem,
  toSuggestion
} from './mcp-resource'

const SAMPLE_RESOURCES: McpResourceItem[] = [
  {
    server: 'fixture',
    uri: 'fixture://state',
    name: 'Application State',
    description: 'Current app state'
  },
  {
    server: 'postgres',
    uri: 'postgres://schema/users',
    name: 'Users Table Schema',
    description: 'PostgreSQL schema for users table'
  },
  {
    server: 'sqlite',
    uri: 'sqlite://migrations/001.sql',
    name: 'Initial Migration',
    description: 'SQL migration script'
  }
]

describe('listMcpResources (lists)', () => {
  beforeEach(() => {
    invalidateMcpResourceSuggestionIndex()
  })

  it('fetches resources via mcp.resources.list and caches results', async () => {
    const mockRequest = vi.fn().mockResolvedValue({
      resources: SAMPLE_RESOURCES
    })

    $gateway.set({ request: mockRequest } as any)

    const list1 = await listMcpResources()
    expect(list1).toHaveLength(3)
    expect(list1[0].uri).toBe('fixture://state')
    expect(mockRequest).toHaveBeenCalledTimes(1)
    expect(mockRequest).toHaveBeenCalledWith('mcp.resources.list', {})

    // Second call served from cache
    const list2 = await listMcpResources()
    expect(list2).toEqual(list1)
    expect(mockRequest).toHaveBeenCalledTimes(1)

    // After invalidation, requests again
    invalidateMcpResourceSuggestionIndex()
    await listMcpResources()
    expect(mockRequest).toHaveBeenCalledTimes(2)
  })

  it('filters by server when requested', async () => {
    const mockRequest = vi.fn().mockResolvedValue({
      resources: SAMPLE_RESOURCES
    })
    $gateway.set({ request: mockRequest } as any)

    // Populate cache
    await listMcpResources()

    const pgResources = await listMcpResources('postgres')
    expect(pgResources).toHaveLength(1)
    expect(pgResources[0].server).toBe('postgres')
  })
})

describe('buildMcpResourceSuggestionIndex and filtering (filters)', () => {
  it('extracts keywords from name, uri scheme, and path segments', () => {
    const index = buildMcpResourceSuggestionIndex(SAMPLE_RESOURCES)
    expect(index).toHaveLength(3)

    const stateEntry = index.find(e => e.uri === 'fixture://state')!
    expect(stateEntry.keywords).toContain('application state')
    expect(stateEntry.keywords).toContain('application')
    expect(stateEntry.keywords).toContain('state')
    expect(stateEntry.keywords).toContain('fixture://state')

    const usersEntry = index.find(e => e.uri === 'postgres://schema/users')!
    expect(usersEntry.keywords).toContain('users')
    expect(usersEntry.keywords).toContain('schema')
  })

  it('detects existing resource references with various quoting', () => {
    expect(draftContainsResource('check @resource:fixture://state please', 'fixture://state')).toBe(true)
    expect(draftContainsResource('see @resource:"fixture://state"', 'fixture://state')).toBe(true)
    expect(draftContainsResource("see @resource:'fixture://state'", 'fixture://state')).toBe(true)
    expect(draftContainsResource('see @resource:`fixture://state`', 'fixture://state')).toBe(true)
    expect(draftContainsResource('no resource here', 'fixture://state')).toBe(false)
  })

  const index = buildMcpResourceSuggestionIndex(SAMPLE_RESOURCES)

  it('matches URI typed in draft', () => {
    const matches = matchResourceSuggestions('please inspect fixture://state', index)
    expect(matches).toEqual([
      {
        server: 'fixture',
        uri: 'fixture://state',
        name: 'Application State',
        keyword: 'fixture://state'
      }
    ])
  })

  it('matches completed whole-word keywords', () => {
    const matches = matchResourceSuggestions('check the schema please', index)
    expect(matches).toEqual([
      {
        server: 'postgres',
        uri: 'postgres://schema/users',
        name: 'Users Table Schema',
        keyword: 'schema'
      }
    ])
  })

  it('is case-insensitive against draft', () => {
    const matches = matchResourceSuggestions('look at the USERS schema ', index)
    expect(matches.some(m => m.uri === 'postgres://schema/users')).toBe(true)
  })

  it('does not match inside unrelated words', () => {
    const matches = matchResourceSuggestions('financial statement analysis ', index)
    expect(matches.some(m => m.uri === 'fixture://state')).toBe(false)
  })

  it('skips resources already referenced in draft', () => {
    const matches = matchResourceSuggestions(
      'here is @resource:fixture:fixture://state for the state ',
      index
    )
    expect(matches.some(m => m.uri === 'fixture://state')).toBe(false)
  })

  it('caps matches at MAX_MATCHES (2)', () => {
    const matches = matchResourceSuggestions('check state and users and migration ', index)
    expect(matches.length).toBeLessThanOrEqual(2)
  })
})

describe('insertResourceReference (inserts the reference)', () => {
  it('inserts resource reference chip and requests focus', () => {
    const insertSpy = vi.spyOn(composerFocus, 'requestComposerInsertRefs').mockImplementation(() => {})
    const focusSpy = vi.spyOn(composerFocus, 'requestComposerFocus').mockImplementation(() => {})

    insertResourceReference('postgres', 'postgres://schema/users', 'Users Table Schema')

    expect(insertSpy).toHaveBeenCalledWith([
      {
        kind: 'resource',
        value: 'postgres:postgres://schema/users',
        label: 'Users Table Schema'
      }
    ])
    expect(focusSpy).toHaveBeenCalled()

    insertSpy.mockRestore()
    focusSpy.mockRestore()
  })

  it('invoking suggestion triggers reference insertion', async () => {
    const insertSpy = vi.spyOn(composerFocus, 'requestComposerInsertRefs').mockImplementation(() => {})
    const focusSpy = vi.spyOn(composerFocus, 'requestComposerFocus').mockImplementation(() => {})

    const suggestion = toSuggestion({
      server: 'fixture',
      uri: 'fixture://state',
      name: 'Application State',
      keyword: 'state'
    })

    expect(suggestion.id).toBe('fixture://state')
    expect(suggestion.provider).toBe('mcp-resource')
    expect(suggestion.label).toBe('Attach Application State')

    await suggestion.invoke({ cancelled: () => false, sessionId: null })

    expect(insertSpy).toHaveBeenCalledWith([
      {
        kind: 'resource',
        value: 'fixture:fixture://state',
        label: 'Application State'
      }
    ])
    expect(focusSpy).toHaveBeenCalled()

    insertSpy.mockRestore()
    focusSpy.mockRestore()
  })
})

describe('composer.atCompletions registration', () => {
  it('provides matching resources for @ query', async () => {
    const mockRequest = vi.fn().mockResolvedValue({
      resources: SAMPLE_RESOURCES
    })
    $gateway.set({ request: mockRequest } as any)

    // Pre-populate index
    await listMcpResources()

    const contributions = registry.getArea(COMPOSER_AREAS.atCompletions)
    const mcpSourceContrib = contributions.find(c => c.id === 'mcp-resources')
    expect(mcpSourceContrib).toBeDefined()

    const source = mcpSourceContrib?.data as ComposerAtCompletionSource
    expect(source).toBeDefined()

    // 1. Empty query returns all resources
    const all = source.provide('')
    expect(all).toHaveLength(3)
    expect(all.map(i => i.insert)).toContain('@resource:fixture:fixture://state')
    expect(all.map(i => i.insert)).toContain('@resource:postgres:postgres://schema/users')

    // 2. Query matching name or keyword
    const filtered = source.provide('schema')
    expect(filtered).toHaveLength(1)
    expect(filtered[0].insert).toBe('@resource:postgres:postgres://schema/users')
    expect(filtered[0].display).toBe('Users Table Schema')
    expect(filtered[0].meta).toContain('postgres')

    // 3. Query with resource: prefix
    const prefixed = source.provide('resource:state')
    expect(prefixed).toHaveLength(1)
    expect(prefixed[0].insert).toBe('@resource:fixture:fixture://state')
  })
})
