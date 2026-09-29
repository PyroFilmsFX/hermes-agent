import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  isValidScopeSubject,
  MAIN_ISSUED_CLASSES,
  needsSubject,
  normalizeStageQuestionBody,
  SCOPE_CATALOG,
  SCOPE_RE,
  SCOPE_TTL_POLICY,
  scopeClassOf,
  scopeTtlOptions,
  scopeTtlPolicy,
  stageQuestionSha256,
  stageVariantSubject,
  SUBJECT_GRAMMAR,
  SUBJECT_REQUIRED_CLASSES
} from './scopes'

const REPO = path.resolve(__dirname, '../../../../..')
const catalog = JSON.parse(fs.readFileSync(path.join(REPO, 'hermes_owner_grant/scopes.json'), 'utf8'))
const vectors = JSON.parse(fs.readFileSync(path.join(REPO, 'tests/fixtures/owner_grant_subject_vectors.json'), 'utf8'))

describe('scope TTL policy', () => {
  it('uses the quote-only class for a forward with no conductor scope', () => {
    expect(scopeTtlPolicy([])).toEqual({ defaultTtlMs: 604_800_000, maxTtlMs: 604_800_000 })
    expect(scopeTtlOptions([])).toContain(604_800_000)
  })

  it('never offers more than a conductor class allows', () => {
    expect(Math.max(...scopeTtlOptions(['conductor:marker:restore']))).toBe(14_400_000)
    expect(Math.max(...scopeTtlOptions(['conductor:gc:prune-lanes']))).toBe(3_600_000)
    expect(Math.max(...scopeTtlOptions(['conductor:spend:fly']))).toBe(3_600_000)
  })
})

describe('b9 parity with hermes_owner_grant/scopes.json', () => {
  it('SCOPE_TTL_POLICY and the subject-required set mirror every conductor class', () => {
    const classes = Object.entries(catalog.classes as Record<string, any>).filter(([name]) => name !== 'quote-only')
    expect(Object.keys(SCOPE_TTL_POLICY).sort()).toEqual(classes.map(([name]) => name).sort())

    for (const [name, policy] of classes) {
      expect(SCOPE_TTL_POLICY[name as keyof typeof SCOPE_TTL_POLICY], name).toEqual({
        defaultTtlMs: policy.default_ttl_ms,
        maxTtlMs: policy.max_ttl_ms
      })
      expect(SUBJECT_REQUIRED_CLASSES.has(name as any), name).toBe(policy.subject_required)
    }
  })

  it('SCOPE_CATALOG is the catalog minus the main-issued continuity scope', () => {
    const requestable = (catalog.scopes as Array<{ scope: string; label: string }>)
      .filter(e => !MAIN_ISSUED_CLASSES.has(e.scope.split(':')[1] as any))
      .map(e => ({ value: e.scope, label: e.label }))
    expect(SCOPE_CATALOG).toEqual(requestable)
    expect(SCOPE_CATALOG.some(e => e.value === 'conductor:continuity:session-relaunch')).toBe(false)
  })

  it('SCOPE_RE takes every requestable class and refuses continuity', () => {
    for (const cls of ['allowlist', 'gate', 'marker', 'prod', 'answer', 'defer', 'override', 'gc', 'policy', 'spend']) {
      expect(SCOPE_RE.test(`conductor:${cls}:x`), cls).toBe(true)
    }

    expect(SCOPE_RE.test('conductor:continuity:session-relaunch')).toBe(false)
    expect(scopeClassOf('conductor:continuity:session-relaunch')).toBeNull()
  })

  it('needsSubject covers every subject-required class', () => {
    for (const entry of SCOPE_CATALOG) {
      const cls = scopeClassOf(entry.value)!
      expect(needsSubject([entry.value]), entry.value).toBe(catalog.classes[cls].subject_required)
    }

    expect(needsSubject([])).toBe(false)
    expect(needsSubject(['conductor:gate:review-budget-enable', 'conductor:answer:stage-variant'])).toBe(true)
  })
})

describe('b9 subject grammar and the stage-question hash (shared vectors)', () => {
  it('grammar patterns and valid/invalid subjects match the vectors', () => {
    for (const [scope, v] of Object.entries(vectors.scopes as Record<string, any>)) {
      expect(SUBJECT_GRAMMAR[scope], scope).toBe(v.grammar)

      for (const good of [v.example, ...v.valid]) {
        expect(isValidScopeSubject(scope, good), `${scope} ${good}`).toBe(true)
      }

      for (const bad of v.invalid) {
        expect(isValidScopeSubject(scope, bad), `${scope} ${JSON.stringify(bad)}`).toBe(false)
      }
    }
  })

  it('normalization vectors: CRLF, space/tab/LF trim only, no Unicode normalization', () => {
    for (const v of vectors.normalization.vectors) {
      expect(normalizeStageQuestionBody(v.raw), v.name).toBe(v.normalized)
      expect(stageQuestionSha256(v.raw), v.name).toBe(v.sha256)
    }

    // A lone CR and a no-break space survive.
    expect(normalizeStageQuestionBody('\rq ')).toBe('\rq ')
  })

  it('the answer vector: question sha and the option-2 subject', () => {
    const q = vectors.scopes['conductor:answer:stage-variant'].question
    expect(stageQuestionSha256(q.body)).toBe(q.question_sha256)
    expect(stageVariantSubject(q.question_sha256, 2)).toBe(q.option_2_subject)

    for (const bad of [0, -1, 1.5, Number.NaN]) {
      expect(() => stageVariantSubject(q.question_sha256, bad)).toThrow(RangeError)
    }

    expect(() => stageVariantSubject('abc', 1)).toThrow(RangeError)
  })
})
