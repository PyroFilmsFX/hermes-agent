/**
 * #60 owner-forward scope picker data. Mirrors `hermes_owner_grant/scopes.json` and
 * electron/owner-grant-sign.ts SCOPE_LABELS for display only; main re-validates every scope against
 * the grammar and shows each one in the native confirm.
 */
export type ScopeClass = 'allowlist' | 'gate' | 'marker' | 'prod'

export const SCOPE_RE = /^conductor:(allowlist|gate|marker|prod):[a-z0-9][a-z0-9-]{0,62}$/

export const SCOPE_CATALOG: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'conductor:allowlist:member-profile', label: 'Allowlisted member profile' },
  { value: 'conductor:gate:job-store-write-block', label: 'Block job store writes' },
  { value: 'conductor:gate:lane-test-budget-enable', label: 'Enable lane test budget' },
  { value: 'conductor:gate:pr-discipline-enable', label: 'Enable PR discipline' },
  { value: 'conductor:gate:review-budget-enable', label: 'Enable review budget' },
  { value: 'conductor:marker:bypass', label: 'Bypass marker' },
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
