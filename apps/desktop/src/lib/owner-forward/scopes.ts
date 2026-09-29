/**
 * #60 owner-forward scope picker data. Mirrors `hermes_owner_grant/scopes.json` and
 * electron/owner-grant-sign.ts SCOPE_LABELS for display only; main re-validates every scope against
 * the grammar and shows each one in the native confirm.
 */
import { sha256Hex } from './sha256'

export type ScopeClass =
  | 'allowlist'
  | 'answer'
  | 'continuity'
  | 'defer'
  | 'gate'
  | 'gc'
  | 'marker'
  | 'override'
  | 'policy'
  | 'prod'
  | 'spend'

export interface ScopeTtlPolicy {
  defaultTtlMs: number
  maxTtlMs: number
}

/** Renderer mirror of classes in hermes_owner_grant/scopes.json. */
export const SCOPE_TTL_POLICY: Readonly<Record<ScopeClass, ScopeTtlPolicy>> = {
  allowlist: { defaultTtlMs: 43_200_000, maxTtlMs: 259_200_000 },
  answer: { defaultTtlMs: 3_600_000, maxTtlMs: 14_400_000 },
  continuity: { defaultTtlMs: 900_000, maxTtlMs: 900_000 },
  defer: { defaultTtlMs: 3_600_000, maxTtlMs: 14_400_000 },
  gate: { defaultTtlMs: 43_200_000, maxTtlMs: 259_200_000 },
  gc: { defaultTtlMs: 3_600_000, maxTtlMs: 3_600_000 },
  marker: { defaultTtlMs: 3_600_000, maxTtlMs: 14_400_000 },
  override: { defaultTtlMs: 3_600_000, maxTtlMs: 14_400_000 },
  policy: { defaultTtlMs: 900_000, maxTtlMs: 3_600_000 },
  prod: { defaultTtlMs: 900_000, maxTtlMs: 3_600_000 },
  spend: { defaultTtlMs: 900_000, maxTtlMs: 3_600_000 }
}

/** Classes whose every scope is single-use and needs a signed subject (scopes.json `classes`). */
export const SUBJECT_REQUIRED_CLASSES: ReadonlySet<ScopeClass> = new Set<ScopeClass>([
  'answer',
  'continuity',
  'defer',
  'gc',
  'override',
  'policy',
  'prod',
  'spend'
])

/** Classes only Electron main issues (the relaunch continuity grant). The renderer can't request
 *  them: SCOPE_RE rejects them and SCOPE_CATALOG omits them; main refuses them regardless. */
export const MAIN_ISSUED_CLASSES: ReadonlySet<ScopeClass> = new Set<ScopeClass>(['continuity'])

// Mirrors scopes.json's "quote-only" class: a forward with no conductor scope.
const QUOTE_ONLY_TTL_POLICY: ScopeTtlPolicy = { defaultTtlMs: 604_800_000, maxTtlMs: 604_800_000 }

const TTL_PRESETS_MS = [900_000, 3_600_000, 14_400_000, 43_200_000, 86_400_000, 259_200_000, 604_800_000]

export function scopeTtlPolicy(scopes: readonly string[]): ScopeTtlPolicy {
  const classes = scopes.map(scopeClassOf).filter((value): value is ScopeClass => value !== null)

  if (!classes.length) {
    return QUOTE_ONLY_TTL_POLICY
  }

  const selected = classes

  return {
    defaultTtlMs: Math.min(...selected.map(scopeClass => SCOPE_TTL_POLICY[scopeClass].defaultTtlMs)),
    maxTtlMs: Math.min(...selected.map(scopeClass => SCOPE_TTL_POLICY[scopeClass].maxTtlMs))
  }
}

export function scopeTtlOptions(scopes: readonly string[]): number[] {
  const policy = scopeTtlPolicy(scopes)
  const options = TTL_PRESETS_MS.filter(value => value <= policy.maxTtlMs)

  if (!options.includes(policy.defaultTtlMs)) {
    options.push(policy.defaultTtlMs)
  }

  if (!options.includes(policy.maxTtlMs)) {
    options.push(policy.maxTtlMs)
  }

  return options.sort((a, b) => a - b)
}

/** The REQUESTABLE scope grammar: hermes_owner_grant/scopes.py `_SCOPE_RE` minus the main-issued
 *  `continuity` class. */
export const SCOPE_RE = /^conductor:(allowlist|gate|marker|prod|answer|defer|override|gc|policy|spend):[a-z0-9][a-z0-9-]{0,62}$/

export const SCOPE_CATALOG: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'conductor:allowlist:member-profile', label: 'Allowlisted member profile' },
  { value: 'conductor:answer:stage-variant', label: 'Answer a conductor stage question' },
  { value: 'conductor:defer:wave-or-unit', label: 'Defer a planned wave or unit' },
  { value: 'conductor:gate:job-store-write-block', label: 'Block job store writes' },
  { value: 'conductor:gate:lane-test-budget-enable', label: 'Enable lane test budget' },
  { value: 'conductor:gate:pr-discipline-enable', label: 'Enable PR discipline' },
  { value: 'conductor:gate:review-budget-enable', label: 'Enable review budget' },
  { value: 'conductor:gc:prune-lanes', label: 'Prune reviewed lanes' },
  { value: 'conductor:marker:bypass', label: 'Bypass marker' },
  { value: 'conductor:marker:rebind-owner', label: 'Rebind marker owner' },
  { value: 'conductor:marker:repoint-ledger', label: 'Repoint marker ledger' },
  { value: 'conductor:marker:restore', label: 'Restore marker' },
  { value: 'conductor:override:review-budget', label: 'Override the review budget' },
  { value: 'conductor:policy:standing-approval', label: 'Enable a standing-approval rule' },
  { value: 'conductor:policy:unsandboxed-write', label: 'Allow unsandboxed writes for a CLI' },
  { value: 'conductor:prod:target', label: 'Production target' },
  { value: 'conductor:spend:fly', label: 'Start paid Fly lanes' }
]

// Per-scope signed-subject grammar: the same patterns as hermes_owner_grant/scopes.py
// SUBJECT_GRAMMAR and electron/owner-grant-sign.ts (tests/fixtures/owner_grant_subject_vectors.json
// pins all three). Each must match the WHOLE subject. Display-side only; main and the verifier
// enforce it.
const SUBJECT_ID = '[A-Za-z0-9][A-Za-z0-9._-]{0,127}'
const SUBJECT_HEX64 = '[0-9a-f]{64}'

export const SUBJECT_GRAMMAR: Readonly<Record<string, string>> = Object.freeze({
  'conductor:answer:stage-variant': `${SUBJECT_HEX64}:[1-9][0-9]*`,
  'conductor:continuity:session-relaunch': `(${SUBJECT_ID}):(?!\\1$)${SUBJECT_ID}`,
  'conductor:defer:wave-or-unit': `${SUBJECT_ID}:${SUBJECT_ID}`,
  'conductor:gc:prune-lanes': SUBJECT_HEX64,
  'conductor:override:review-budget': `${SUBJECT_ID}:${SUBJECT_ID}:(?:fan|recheck)`,
  'conductor:policy:standing-approval': `${SUBJECT_ID}:${SUBJECT_HEX64}`,
  'conductor:policy:unsandboxed-write': '[a-z0-9][a-z0-9._-]{0,63}',
  'conductor:spend:fly': '[a-z0-9][a-z0-9-]{0,62}:(?![0.]+$)(?:0|[1-9][0-9]{0,8})(?:\\.[0-9]{1,2})?'
})

const SUBJECT_RES: ReadonlyMap<string, RegExp> = new Map(
  Object.entries(SUBJECT_GRAMMAR).map(([scope, pattern]) => [scope, new RegExp(`^(?:${pattern})$`)])
)

/** Whether `subject` fits `scope`'s signed-subject grammar (true for a scope with no grammar). */
export function isValidScopeSubject(scope: string, subject: string): boolean {
  const re = SUBJECT_RES.get(scope)

  return re ? typeof subject === 'string' && re.test(subject) : true
}

/** The `:::stage-question` body normalization (brief b9 §5): CRLF -> LF, then trim only U+0020,
 *  U+0009 and U+000A at both ends. No Unicode normalization; a lone CR is kept. */
export function normalizeStageQuestionBody(body: string): string {
  return body.replace(/\r\n/g, '\n').replace(/^[ \t\n]+|[ \t\n]+$/g, '')
}

/** `question_sha256`: lowercase hex SHA-256 of the UTF-8 bytes of the normalized body. */
export function stageQuestionSha256(body: string): string {
  return sha256Hex(normalizeStageQuestionBody(body))
}

/** The `conductor:answer:stage-variant` subject for a 1-based option. Throws on a bad index or hash. */
export function stageVariantSubject(questionSha256: string, optionIndex: number): string {
  const subject = `${questionSha256}:${optionIndex}`

  if (!Number.isSafeInteger(optionIndex) || optionIndex < 1 || !isValidScopeSubject('conductor:answer:stage-variant', subject)) {
    throw new RangeError('stage-variant subject needs a 64-hex question_sha256 and a 1-based option index')
  }

  return subject
}

export function scopeClassOf(value: string): ScopeClass | null {
  const match = SCOPE_RE.exec(value)

  return match ? (match[1] as ScopeClass) : null
}

export function scopeLabel(value: string): string {
  return SCOPE_CATALOG.find(entry => entry.value === value)?.label ?? value
}

export function needsSubject(scopes: readonly string[]): boolean {
  return scopes.some(scope => {
    const scopeClass = scopeClassOf(scope)

    return scopeClass !== null && SUBJECT_REQUIRED_CLASSES.has(scopeClass)
  })
}
