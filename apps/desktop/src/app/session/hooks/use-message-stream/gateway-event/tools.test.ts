import { beforeEach, describe, expect, it, vi } from 'vitest'

const { invalidateSlashCompletions } = vi.hoisted(() => ({
  invalidateSlashCompletions: vi.fn()
}))
const { invalidateSkillSuggestionIndex } = vi.hoisted(() => ({
  invalidateSkillSuggestionIndex: vi.fn()
}))
const { refreshBackgroundProcesses } = vi.hoisted(() => ({
  refreshBackgroundProcesses: vi.fn(async () => undefined)
}))
const { pruneDelegateFallbackSubagents, upsertSubagent } = vi.hoisted(() => ({
  pruneDelegateFallbackSubagents: vi.fn(),
  upsertSubagent: vi.fn()
}))

vi.mock('@/lib/slash-completion-cache', () => ({ invalidateSlashCompletions }))
vi.mock('@/store/suggestion-providers/skill', () => ({ invalidateSkillSuggestionIndex }))
vi.mock('@/store/composer-status', () => ({ refreshBackgroundProcesses }))
vi.mock('@/store/subagents', () => ({ pruneDelegateFallbackSubagents, upsertSubagent }))

import { handleToolEvent } from './tools'
import type { GatewayEventContext } from './types'

function makeToolContext(
  toolName: string,
  sessionId = 's1',
  options: {
    eventType?: GatewayEventContext['event']['type']
    interrupted?: boolean
    payload?: Record<string, unknown>
  } = {}
): GatewayEventContext {
  return {
    deps: {
      flushQueuedDeltas: vi.fn(),
      nativeSubagentSessionsRef: { current: new Set() },
      sessionInterrupted: vi.fn(() => options.interrupted ?? false),
      updateSessionState: vi.fn(),
      upsertToolCall: vi.fn()
    } as unknown as GatewayEventContext['deps'],
    event: { type: options.eventType ?? 'tool.complete' },
    explicitSid: sessionId,
    fromActiveSource: () => true,
    isActiveEvent: false,
    occurredAt: 1_700_000_100,
    payload: options.payload ?? { name: toolName },
    scheduleConfigRefresh: vi.fn(),
    sessionId
  }
}

describe('handleToolEvent tool name normalization', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it.each([
    'skill_manage',
    'mcp__hermes-tools__skill_manage',
    'mcp__hermes-hybrid__skill_manage'
  ])('invalidates slash completions and skill suggestion index for %s', toolName => {
    expect(handleToolEvent(makeToolContext(toolName))).toBe(true)
    expect(invalidateSlashCompletions).toHaveBeenCalledTimes(1)
    expect(invalidateSkillSuggestionIndex).toHaveBeenCalledTimes(1)
  })

  it('does not invalidate skill completions for unrelated or non-hermes tools', () => {
    expect(handleToolEvent(makeToolContext('mcp__other-server__skill_manage'))).toBe(true)
    expect(invalidateSlashCompletions).not.toHaveBeenCalled()
    expect(invalidateSkillSuggestionIndex).not.toHaveBeenCalled()
  })

  it('accepts subagent lifecycle events while the parent turn is interrupted', () => {
    const ctx = makeToolContext('', 's1', {
      eventType: 'subagent.complete',
      interrupted: true,
      payload: { subagent_id: 'child', goal: 'Background research', status: 'completed' }
    })

    expect(handleToolEvent(ctx)).toBe(true)
    expect(upsertSubagent).toHaveBeenCalledWith(
      's1',
      { subagent_id: 'child', goal: 'Background research', status: 'completed' },
      false,
      'subagent.complete'
    )
  })

  it.each([
    'terminal',
    'process',
    'mcp__hermes-tools__terminal',
    'mcp__hermes-hybrid__process'
  ])('refreshes background processes for %s', toolName => {
    expect(handleToolEvent(makeToolContext(toolName))).toBe(true)
    expect(refreshBackgroundProcesses).toHaveBeenCalledWith('s1')
  })

  it.each([
    'mcp__other-server__terminal',
    'mcp__other-server__process',
    'mcp__hermes_tools__skill_manage',
    'mcp__hermes-toolsX__terminal'
  ])('ignores non-Hermes or misspelled server namespaces (%s)', toolName => {
    expect(handleToolEvent(makeToolContext(toolName))).toBe(true)
    expect(refreshBackgroundProcesses).not.toHaveBeenCalled()
    expect(invalidateSlashCompletions).not.toHaveBeenCalled()
    expect(invalidateSkillSuggestionIndex).not.toHaveBeenCalled()
  })
})
