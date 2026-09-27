export type SessionRole = 'manager' | 'orchestrator' | 'stream'

/**
 * Pure function to derive session role from title.
 * Strips leading "hermes:" and matches trailing -manager / -orchestrator / -stream
 * (case-insensitive), also accepting the bare title "manager".
 */
export function sessionRole(title: string | null | undefined): SessionRole | null {
  if (!title || typeof title !== 'string') {
    return null
  }

  const cleaned = title.trim().replace(/^hermes:/i, '').trim()

  if (!cleaned) {
    return null
  }

  if (cleaned.toLowerCase() === 'manager') {
    return 'manager'
  }

  const match = cleaned.match(/-(manager|orchestrator|stream)$/i)

  if (match) {
    return match[1].toLowerCase() as SessionRole
  }

  return null
}
