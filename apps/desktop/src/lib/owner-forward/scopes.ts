/**
 * #60 owner-forward scope picker data. Mirrors `hermes_owner_grant/scopes.json` and
 * electron/owner-grant-sign.ts SCOPE_LABELS for display only; main re-validates every scope against
 * the grammar and shows each one in the native confirm.
 */
export type ScopeClass = 'allowlist' | 'gate' | 'marker' | 'prod'

export interface ScopeTtlPolicy {
  defaultTtlMs: number
  maxTtlMs: number
}

/** Renderer mirror of classes in hermes_owner_grant/scopes.json. */
export const SCOPE_TTL_POLICY: Readonly<Record<ScopeClass, ScopeTtlPolicy>> = {
  allowlist: { defaultTtlMs: 43_200_000, maxTtlMs: 259_200_000 },
  gate: { defaultTtlMs: 43_200_000, maxTtlMs: 259_200_000 },
  marker: { defaultTtlMs: 3_600_000, maxTtlMs: 14_400_000 },
  prod: { defaultTtlMs: 900_000, maxTtlMs: 3_600_000 }
}

const TTL_PRESETS_MS = [900_000, 3_600_000, 14_400_000, 43_200_000, 86_400_000, 259_200_000]

export function scopeTtlPolicy(scopes: readonly string[]): ScopeTtlPolicy {
  const classes = scopes.map(scopeClassOf).filter((value): value is ScopeClass => value !== null)
  const selected = classes.length ? classes : ['allowlist' as const]

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

export const SCOPE_RE = /^conductor:(allowlist|gate|marker|prod):[a-z0-9][a-z0-9-]{0,62}$/

export const SCOPE_CATALOG: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'conductor:allowlist:member-profile', label: 'Allowlisted member profile' },
  { value: 'conductor:gate:job-store-write-block', label: 'Block job store writes' },
  { value: 'conductor:gate:lane-test-budget-enable', label: 'Enable lane test budget' },
  { value: 'conductor:gate:pr-discipline-enable', label: 'Enable PR discipline' },
  { value: 'conductor:gate:review-budget-enable', label: 'Enable review budget' },
  { value: 'conductor:marker:bypass', label: 'Bypass marker' },
  { value: 'conductor:marker:rebind-owner', label: 'Rebind marker owner' },
  { value: 'conductor:marker:repoint-ledger', label: 'Repoint marker ledger' },
  { value: 'conductor:marker:restore', label: 'Restore marker' },
  { value: 'conductor:prod:target', label: 'Production target' }
]

export function scopeClassOf(value: string): ScopeClass | null {
  const match = SCOPE_RE.exec(value)

  return match ? (match[1] as ScopeClass) : null
}

export function scopeLabel(value: string): string {
  return SCOPE_CATALOG.find(entry => entry.value === value)?.label ?? value
}

export function needsSubject(scopes: readonly string[]): boolean {
  return scopes.some(scope => scopeClassOf(scope) === 'prod')
}
