import { describe, expect, it } from 'vitest'

import { exitNote, kindLabel, modelLabel, seatFor, seatLabel, seatMonogram, statusLabel, statusTone } from './conductor-seat'

describe('conductor seat vocabulary', () => {
  it.each([
    ['gpt-6-luna', undefined, 'GPT-6 Luna'],
    ['gpt-6-sol', 'high', 'GPT-6 Sol'],
    ['gemini-3.8-flash-medium', 'medium', 'Gemini 3.8 Flash'],
    ['grok-4.6', undefined, 'Grok 4.6'],
    ['claude-opus-5-5', undefined, 'Opus 5.5']
  ])('labels %s as %s', (model, effort, expected) => {
    expect(modelLabel(model, effort)).toBe(expected)
  })

  it('never labels an unattested model', () => {
    expect(modelLabel('')).toBe('')
    expect(modelLabel('unverified')).toBe('')
    expect(modelLabel('backend-does-not-attest')).toBe('')
  })

  it('treats agy and gemini as one seat and claude models as the claude seat', () => {
    expect(seatFor('agy')).toBe('agy')
    expect(seatFor('gemini')).toBe(seatFor('agy'))
    expect(seatMonogram('gemini')).toBe('ag')
    expect(seatLabel('gemini')).toBe('agy')
    expect(seatFor('codex')).toBe('codex')
    expect(seatMonogram('codex')).toBe('cx')
    expect(seatLabel('codex')).toBe('Codex')
    expect(seatMonogram('grok')).toBe('gk')
    expect(seatFor('relay', 'claude-opus-5-5')).toBe('claude')
    expect(seatMonogram('relay', 'claude-opus-5-5')).toBe('cl')
  })

  it('gives an unknown worker a neutral two-letter monogram and its raw label', () => {
    expect(seatFor('mistral')).toBe('other')
    expect(seatMonogram('mistral')).toBe('mi')
    expect(seatLabel('mistral')).toBe('mistral')
  })

  it.each([
    ['impl', 'Build'],
    ['fix', 'Fix'],
    ['validate', 'Review'],
    ['research', 'Research'],
    ['council', 'Council']
  ])('maps lane %s to kind %s', (lane, kind) => {
    expect(kindLabel(lane)).toBe(kind)
  })

  it.each([
    ['running', 'live', 'Running'],
    ['active', 'live', 'Running'],
    ['waiting', 'wait', 'Waiting'],
    ['succeeded', 'ok', 'Done'],
    ['stale', 'attention', 'Not responding'],
    ['timeout', 'attention', 'Timed out'],
    ['lease_expired', 'attention', 'Lease expired'],
    ['failed', 'stop', 'Failed'],
    ['blocked', 'stop', 'Blocked'],
    ['cancelled', 'stop', 'Cancelled'],
    ['exploded', 'attention', 'Exploded']
  ])('status %s has tone %s and label %s', (status, tone, label) => {
    expect(statusTone(status)).toBe(tone)
    expect(statusLabel(status)).toBe(label)
  })

  it('answers council rows with Answered', () => {
    expect(statusLabel('succeeded', 'council')).toBe('Answered')
  })

  it('adds an exit note only when it says something', () => {
    expect(exitNote('failed', 1)).toBe('exit 1')
    expect(exitNote('failed', undefined)).toBe('no exit code')
    expect(exitNote('timeout', -15)).toBe('stopped after the time limit')
    expect(exitNote('succeeded', 0)).toBeNull()
    expect(exitNote('running', undefined)).toBeNull()
  })
})
