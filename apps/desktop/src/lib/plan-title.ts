/**
 * A readable plan title from the marker's plan file stem, without reading the plan.
 * `OVERNIGHT-HERMES-WORKER-2026-09-26` → `Overnight Hermes Worker`.
 */
export function planTitle(stem: string): string {
  const trimmed = stem.trim().replace(/\.md$/i, '')

  if (trimmed.toLowerCase() === 'tb-plan') {
    return 'Build plan'
  }

  const words = trimmed
    .replace(/[-_]\d{4}-\d{2}-\d{2}$/, '')
    .split(/[-_]+/)
    .filter(Boolean)
    .map(token => {
      if (token.length <= 3 && (/\d/.test(token) || token === token.toUpperCase())) {
        return token
      }

      return token.charAt(0).toUpperCase() + token.slice(1).toLowerCase()
    })

  return words.join(' ') || trimmed
}
