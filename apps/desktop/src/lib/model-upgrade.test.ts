import { describe, expect, it } from 'vitest'

import { modelUpgradeFor, modelUpgradeLabel, modelUpgradeTarget, upgradeFamilyKey } from './model-upgrade'

// The Anthropic curated list as the backend ships it (newest-first per family).
const ANTHROPIC = [
  'claude-fable-5-1',
  'claude-fable-5',
  'claude-opus-5-5',
  'claude-opus-5',
  'claude-opus-5-fast',
  'claude-sonnet-5-5',
  'claude-sonnet-5',
  'claude-haiku-4-5-20251001'
]

// The Claude Agent SDK route lists only 1M-context ids.
const CLAUDE_SDK = ['claude-sonnet-5[1m]', 'claude-opus-5-5[1m]', 'claude-opus-5[1m]', 'claude-haiku-4-5-20251001']

describe('modelUpgradeTarget', () => {
  it('offers Opus 5.5 to a session pinned on Opus 5', () => {
    expect(modelUpgradeTarget('claude-opus-5', ANTHROPIC)).toBe('claude-opus-5-5')
    expect(modelUpgradeLabel('claude-opus-5-5')).toBe('Opus 5.5')
  })

  it('offers Sonnet 5.5 to a session pinned on Sonnet 5', () => {
    expect(modelUpgradeTarget('claude-sonnet-5', ANTHROPIC)).toBe('claude-sonnet-5-5')
    expect(modelUpgradeLabel('claude-sonnet-5-5')).toBe('Sonnet 5.5')
  })

  it('shows nothing for the newest model of a family', () => {
    expect(modelUpgradeTarget('claude-opus-5-5', ANTHROPIC)).toBeNull()
    expect(modelUpgradeTarget('claude-sonnet-5-5', ANTHROPIC)).toBeNull()
    expect(modelUpgradeTarget('claude-fable-5-1', ANTHROPIC)).toBeNull()
  })

  it('maps a [1m] variant to the [1m] newest, never dropping the variant', () => {
    expect(modelUpgradeTarget('claude-opus-5[1m]', CLAUDE_SDK)).toBe('claude-opus-5-5[1m]')
    expect(modelUpgradeLabel('claude-opus-5-5[1m]')).toBe('Opus 5.5 · 1M')
    // Sonnet 5 [1m] is already the newest Sonnet this route lists.
    expect(modelUpgradeTarget('claude-sonnet-5[1m]', CLAUDE_SDK)).toBeNull()
  })

  it('shows no hint when the newest model lacks the same variant', () => {
    // Newest Opus exists only without [1m]: suggesting it would drop the window.
    expect(modelUpgradeTarget('claude-opus-5[1m]', ['claude-opus-5-5', 'claude-opus-5[1m]'])).toBeNull()
    // …and a base session is never pushed onto a [1m] id.
    expect(modelUpgradeTarget('claude-opus-5', ['claude-opus-5-5[1m]', 'claude-opus-5'])).toBeNull()
  })

  it('keeps -fast on -fast, else shows nothing', () => {
    expect(modelUpgradeTarget('claude-opus-5-fast', ANTHROPIC)).toBeNull()
    expect(modelUpgradeTarget('claude-opus-5-fast', [...ANTHROPIC, 'claude-opus-5-5-fast'])).toBe(
      'claude-opus-5-5-fast'
    )
  })

  it('treats a dated snapshot as a deliberate pin', () => {
    expect(modelUpgradeTarget('claude-opus-5-20260101', ANTHROPIC)).toBeNull()
    expect(modelUpgradeTarget('claude-sonnet-4-5-20250929', ANTHROPIC)).toBeNull()
    expect(modelUpgradeTarget('gpt-4o-2024-08-06', ['gpt-6', 'gpt-4o-2024-08-06'])).toBeNull()
  })

  it('never suggests a dated snapshot as the target', () => {
    expect(modelUpgradeTarget('claude-opus-5', ['claude-opus-5-5-20260901', 'claude-opus-5'])).toBeNull()
  })

  it('never suggests (or suggests away from) a banned family', () => {
    expect(modelUpgradeTarget('claude-haiku-4-5', ['claude-haiku-5', 'claude-haiku-4-5'])).toBeNull()
    expect(modelUpgradeTarget('claude-haiku-4-5-20251001', ANTHROPIC)).toBeNull()
  })

  it('works generically for a non-Claude family in the curated list', () => {
    const openai = ['gpt-6', 'gpt-5.5', 'gpt-5.5-mini', 'gpt-5', 'gpt-4o']

    expect(modelUpgradeTarget('gpt-5.5', openai)).toBe('gpt-6')
    expect(modelUpgradeLabel('gpt-6')).toBe('GPT-6')
    expect(modelUpgradeTarget('gpt-5', openai)).toBe('gpt-6')
    // Tiers are their own family: no newer mini is listed.
    expect(modelUpgradeTarget('gpt-5.5-mini', openai)).toBeNull()
    expect(modelUpgradeTarget('gpt-6', openai)).toBeNull()
    // No parseable version, nothing to compare.
    expect(modelUpgradeTarget('gpt-4o', openai)).toBeNull()
  })

  it('never suggests a downgrade from a catalog that is not newest-first', () => {
    const unordered = ['claude-sonnet-4.6', 'claude-sonnet-5', 'claude-sonnet-4']

    expect(modelUpgradeTarget('claude-sonnet-4.6', unordered)).toBe('claude-sonnet-5')
    expect(modelUpgradeTarget('claude-sonnet-5', unordered)).toBeNull()
  })

  it('stays inside the vendor prefix of an aggregator catalog', () => {
    const openrouter = ['anthropic/claude-opus-5.5', 'anthropic/claude-opus-5', 'openai/gpt-6']

    expect(modelUpgradeTarget('anthropic/claude-opus-5', openrouter)).toBe('anthropic/claude-opus-5.5')
    expect(modelUpgradeTarget('claude-opus-5', openrouter)).toBeNull()
  })

  it('handles empty input', () => {
    expect(modelUpgradeTarget('', ANTHROPIC)).toBeNull()
    expect(modelUpgradeTarget('claude-opus-5', [])).toBeNull()
  })
})

describe('upgradeFamilyKey', () => {
  it('reuses the picker family key for Claude and drops version tokens elsewhere', () => {
    expect(upgradeFamilyKey('claude-opus-5-5')).toBe('opus')
    expect(upgradeFamilyKey('claude-opus-4.8')).toBe('opus')
    expect(upgradeFamilyKey('gpt-5.5')).toBe('gpt')
    expect(upgradeFamilyKey('gpt-5.5-mini')).toBe('gpt-mini')
    expect(upgradeFamilyKey('gemini-3.1-pro')).toBe('gemini-pro')
  })
})

describe('modelUpgradeFor', () => {
  it('resolves against the matched provider row and reports its slug', () => {
    expect(modelUpgradeFor('claude-opus-5', { models: ANTHROPIC, slug: 'anthropic' })).toEqual({
      from: 'claude-opus-5',
      provider: 'anthropic',
      to: 'claude-opus-5-5'
    })
    expect(modelUpgradeFor('claude-opus-5', undefined)).toBeNull()
    expect(modelUpgradeFor('', { models: ANTHROPIC, slug: 'anthropic' })).toBeNull()
  })
})

describe('bare id on a catalog that lists only its [1m] variant (Claude Agent SDK route)', () => {
  it('resolves the bare pin to the declared variant and offers that variant of the newest model', () => {
    const sdk = ['claude-opus-5-5[1m]', 'claude-opus-5[1m]', 'claude-sonnet-5-5[1m]', 'claude-sonnet-5[1m]']

    expect(modelUpgradeTarget('claude-opus-5', sdk)).toBe('claude-opus-5-5[1m]')
    expect(modelUpgradeTarget('claude-sonnet-5', sdk)).toBe('claude-sonnet-5-5[1m]')
    expect(modelUpgradeTarget('claude-opus-5-5', sdk)).toBeNull()
  })
})

