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

vi.mock('@/lib/slash-completion-cache', () => ({ invalidateSlashCompletions }))
vi.mock('@/store/suggestion-providers/skill', () => ({ invalidateSkillSuggestionIndex }))
vi.mock('@/store/composer-status', () => ({ refreshBackgroundProcesses }))

import { handleToolEvent } from './tools'
import type { GatewayEventContext } from './types'

function makeToolContext(toolName: string, sessionId = 's1'): GatewayEventContext {
  return {
    deps: {
      flushQueuedDeltas: vi.fn(),
      nativeSubagentSessionsRef: { current: new Set() },
      sessionInterrupted: vi.fn(() => false),
      updateSessionState: vi.fn(),
      upsertToolCall: vi.fn()
    } as unknown as GatewayEventContext['deps'],
    event: { type: 'tool.complete' },
    explicitSid: sessionId,
    fromActiveSource: () => true,
    isActiveEvent: false,
    occurredAt: 1_700_000_100,
    payload: { name: toolName },
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
