/**
 * Session role tags (decision D28).
 *
 * Types:
 * - `manager`      — title `…-manager`, or the bare title `manager`
 * - `orchestrator` — title `…-orchestrator`
 * - `worker`       — title `…-worker`
 * - `stream`       — title `…-stream`
 *
 * Precedence: explicit > name > none.
 * 1. explicit — the owner-set tag persisted on the session row
 *    (`sessions.session_role`, set through PATCH /api/sessions/{id} `role`);
 * 2. name     — derived from the title by {@link sessionRole};
 * 3. none     — no badge.
 * "Auto (from name)" clears the explicit tag, handing the decision back to the
 * title. The role is display metadata only: it never enters the system prompt
 * or the transcript.
 */
export const SESSION_ROLES = ['manager', 'orchestrator', 'worker', 'stream'] as const

export type SessionRole = (typeof SESSION_ROLES)[number]

export function isSessionRole(value: unknown): value is SessionRole {
  return typeof value === 'string' && (SESSION_ROLES as readonly string[]).includes(value)
}

/**
 * Pure function to derive session role from title.
 * Strips leading "hermes:" and matches trailing -manager / -orchestrator /
 * -worker / -stream (case-insensitive), also accepting the bare title "manager".
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

  const match = cleaned.match(/-(manager|orchestrator|worker|stream)$/i)

  if (match) {
    return match[1].toLowerCase() as SessionRole
  }

  return null
}

/** The role the badge shows: the explicit tag when valid, else the name-derived one. */
export function effectiveSessionRole(session: {
  session_role?: null | string
  title?: null | string
}): SessionRole | null {
  return isSessionRole(session.session_role) ? session.session_role : sessionRole(session.title)
}
