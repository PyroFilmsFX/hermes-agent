/**
 * b9 §3: the relaunch continuity issuer. Electron main ONLY.
 *
 * When the app relaunches a profile's backend and a session's live Claude CLI session id changes,
 * main signs ONE `conductor:continuity:session-relaunch` grant so conductor can rebind that
 * session's marker from the old Claude session to the new one:
 *
 * - subject `<old_claude_sid>:<new_claude_sid>` (the verifier's grammar also refuses old == new);
 * - payload extras `hermes_session_id` and `at` (the signing time, ms);
 * - TTL 15 min (the continuity class default and max), single use, touch_id never;
 * - target: the Hermes session bound to the NEW Claude session (the hook's stdin id after the
 *   relaunch), so only that session's live CLI can present it;
 * - audience `hermes-owner-verify` only: never a forward the gateway would deliver as owner text.
 *
 * Where `old` comes from (review P1-1): NEVER state.db. `sessions.claude_sdk_session_id` is
 * agent-writable, so it proves nothing. While a backend runs, main's own timer asks that backend
 * for its sessions' LIVE Claude CLI ids (the CLI's own announcement, `live_claude_cli_session`,
 * surfaced as `claude_session_id` by GET /api/sessions/{id}) and records them here, in main-owned
 * memory, tagged with the backend spawn id. On a relaunch the observations of the PREVIOUS backend
 * are frozen; a grant is issued only when `old` equals that pre-relaunch observation (recent
 * enough) and the relaunched backend's live id differs. No observation, no grant.
 *
 * Non-requestability (review P1-2): observation and minting are driven only by main's backend
 * lifecycle (the observation timer and the spawn hook). No IPC channel, gateway method or tool, and
 * no signing or forward request, reaches this module; `owner-grant-sign.ts` `plan()` and the
 * owner-forward confirm handler both refuse main-issued classes before any side effect
 * (owner-grant-continuity.test.ts pins it).
 */

import { createHash, randomBytes as nodeRandomBytes } from 'node:crypto'
import type nodeFs from 'node:fs'

import type { OwnerGrantEnvelope, OwnerKeyStore } from './owner-grant-key'
import {
  deriveGrantId,
  encodePayload,
  GRANT_VERSION,
  SCOPE_CLASS_POLICY,
  subjectMatchesGrammar,
  writeGrantFile
} from './owner-grant-sign'

export const CONTINUITY_SCOPE = 'conductor:continuity:session-relaunch'
export const CONTINUITY_AUDIENCE: readonly string[] = Object.freeze(['hermes-owner-verify'])
export const CONTINUITY_GESTURE = 'relaunch'
export const CONTINUITY_CONFIRM = 'main_issued'
export const CONTINUITY_TTL_MS = SCOPE_CLASS_POLICY.continuity.default_ttl_ms

/** How often main records live Claude ids while a backend runs, and how old the last pre-relaunch
 *  observation may be (at the relaunch) to count. */
export const CONTINUITY_OBSERVE_MS = 60_000
export const CONTINUITY_OBSERVATION_MAX_AGE_MS = 3 * CONTINUITY_OBSERVE_MS

/** How long after a relaunch main looks for the new Claude session, and how often. */
export const CONTINUITY_WATCH_MS = 10 * 60_000
export const CONTINUITY_POLL_MS = 15_000

/** A live Claude CLI id main itself saw a backend announce for a session. */
export interface ContinuityObservation {
  /** The spawn id of the backend that reported it. */
  backend: string
  claudeSid: string
  at: number
}

export interface ContinuityRelaunch {
  /** The profile whose backend was relaunched (the one main reads through). */
  backendProfile: string
  /** The Hermes session's own profile. */
  profile: string
  hermesSessionId: string
  /** The NEW backend's spawn id (the backend the relaunched session now runs on). */
  backend: string
  oldClaudeSid: string
  newClaudeSid: string
  /** Main's own pre-relaunch observation for this session (from the PREVIOUS backend). */
  observed: ContinuityObservation | null
}

export interface ContinuitySignPorts {
  store: Pick<OwnerKeyStore, 'assertMaySign' | 'signEnvelope'>
  grantsDir: string
  ownerUid: number
  now: () => number
  randomBytes?: (n: number) => Buffer
  fs?: typeof nodeFs
}

export type ContinuityRefusal = 'bad_input' | 'no_observation' | 'not_observed' | 'signing_off'

export type ContinuityOutcome =
  | { issued: true; grantId: string; path: string; envelope: OwnerGrantEnvelope }
  | { issued: false; reason: ContinuityRefusal }

function nonEmpty(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && !value.includes('\u0000')
}

function b32(bytes: Buffer): string {
  const alphabet = 'abcdefghijklmnopqrstuvwxyz234567'
  let bits = 0
  let acc = 0
  let out = ''

  for (const byte of bytes) {
    acc = (acc << 8) | byte
    bits += 8

    while (bits >= 5) {
      out += alphabet[(acc >>> (bits - 5)) & 31]
      bits -= 5
    }
  }

  return bits > 0 ? out + alphabet[(acc << (5 - bits)) & 31] : out
}

/** Sign and write one continuity grant. Refuses (signing nothing) unless the subject fits the
 *  grammar and `old` is exactly main's own pre-relaunch observation of the session's live Claude id,
 *  made by a DIFFERENT (the previous) backend. Not reachable from any IPC, gateway or tool. */
export async function issueContinuityGrant(r: ContinuityRelaunch, ports: ContinuitySignPorts): Promise<ContinuityOutcome> {
  const subject = `${r?.oldClaudeSid}:${r?.newClaudeSid}`

  if (
    !r ||
    !nonEmpty(r.backendProfile) ||
    !nonEmpty(r.profile) ||
    !nonEmpty(r.hermesSessionId) ||
    !nonEmpty(r.backend) ||
    !nonEmpty(r.oldClaudeSid) ||
    !nonEmpty(r.newClaudeSid) ||
    !subjectMatchesGrammar(CONTINUITY_SCOPE, subject)
  ) {
    return { issued: false, reason: 'bad_input' }
  }

  const seen = r.observed

  if (!seen || !nonEmpty(seen.claudeSid) || !nonEmpty(seen.backend)) {
    return { issued: false, reason: 'no_observation' }
  }

  if (seen.claudeSid !== r.oldClaudeSid || seen.backend === r.backend) {
    return { issued: false, reason: 'not_observed' }
  }

  try {
    ports.store.assertMaySign([CONTINUITY_SCOPE])
  } catch {
    return { issued: false, reason: 'signing_off' }
  }

  const random = ports.randomBytes ?? nodeRandomBytes
  const issuedAt = ports.now()
  const text = `Relaunch continuity for session ${r.hermesSessionId}: Claude session ${r.oldClaudeSid} -> ${r.newClaudeSid}`

  const payload = {
    v: GRANT_VERSION,
    aud: [...CONTINUITY_AUDIENCE],
    decision_id: 'od_' + b32(random(16)),
    issued_at: issuedAt,
    deliver_by: issuedAt,
    expires_at: issuedAt + CONTINUITY_TTL_MS,
    owner_uid: ports.ownerUid,
    backend: r.backend,
    nonce: random(16).toString('base64url'),
    gesture: CONTINUITY_GESTURE,
    confirm: CONTINUITY_CONFIRM,
    source_session: { session_id: r.hermesSessionId, message_id: null, role: null },
    targets: [{ session_id: r.hermesSessionId, claude_session_id: r.newClaudeSid }],
    forward_targets: [`${r.profile}:${r.hermesSessionId}`],
    scope: [CONTINUITY_SCOPE],
    single_use: [CONTINUITY_SCOPE],
    subject: { [CONTINUITY_SCOPE]: subject },
    hermes_session_id: r.hermesSessionId,
    at: issuedAt,
    text,
    text_sha256: createHash('sha256').update(text, 'utf8').digest('hex'),
    text_len: Buffer.byteLength(text, 'utf8')
  }

  const bytes = encodePayload(payload)
  const grantId = deriveGrantId(bytes)
  const envelope = ports.store.signEnvelope(bytes, [CONTINUITY_SCOPE])
  const file = writeGrantFile(ports.grantsDir, envelope, issuedAt, grantId, ports.fs)

  return { issued: true, grantId, path: file, envelope }
}

// -- observation + the relaunch watcher (main drives both from its backend lifecycle) ----------

export interface ContinuityLiveSession {
  profile: string
  sessionId: string
  /** The session's LIVE Claude CLI id as the backend announced it (never a state.db value). */
  live: string | null
}

export interface ContinuityIssuerPorts extends ContinuitySignPorts {
  /** Main's own read, through `backendProfile`'s running backend, of its sessions' live Claude ids.
   *  Must not spawn a backend. */
  listLiveSessions: (backendProfile: string) => Promise<ContinuityLiveSession[]>
  /** Main's own read of one session's live Claude id on the (relaunched) backend. */
  readLive: (backendProfile: string, profile: string, hermesSessionId: string) => Promise<string | null>
  setTimer?: (fn: () => void, ms: number) => unknown
  log?: (message: string) => void
}

export interface ContinuityIssuer {
  /** Main's observation timer: record the live Claude ids `backend` (the profile's CURRENT spawn
   *  id) reports now. */
  observe(backendProfile: string, backend: string): Promise<void>
  /** Main's backend spawn hook: `backendProfile`'s backend was RE-launched as `backend`. Freezes the
   *  previous backend's observations and signs one grant per session whose live id changed. */
  onBackendRelaunch(backendProfile: string, backend: string): Promise<ContinuityOutcome[]>
}

export const MAX_OBSERVED = 256

export interface CollectLiveSessionsPorts {
  fetchJson: (path: string) => Promise<unknown>
  readLive: (backendProfile: string, profile: string, sessionId: string) => Promise<string | null>
}

export interface CollectLiveSessionsOptions {
  pageSize?: number
  maxPages?: number
  maxActive?: number
  concurrency?: number
}

/**
 * Paginates GET /api/sessions on the backend to observe every session with a live
 * Claude CLI, without arbitrary 10-count or 20-row caps.
 *
 * Concurrency to readLive is capped at <= 4 (default 4) to prevent N-fanout spikes,
 * and request counts are bounded (maxPages, maxActive). Observations are main-owned
 * via readLive (never state.db columns).
 */
export async function collectLiveSessions(
  backendProfile: string,
  ports: CollectLiveSessionsPorts,
  opts: CollectLiveSessionsOptions = {}
): Promise<ContinuityLiveSession[]> {
  if (!nonEmpty(backendProfile)) {
    return []
  }

  const pageSize = Math.min(100, Math.max(1, opts.pageSize ?? 100))
  const maxPages = Math.max(1, opts.maxPages ?? 10)
  const maxActive = Math.max(1, opts.maxActive ?? MAX_OBSERVED)
  const concurrency = Math.min(4, Math.max(1, opts.concurrency ?? 4))

  const activeCandidates: Array<{ id: string; profile: string }> = []
  const seenIds = new Set<string>()

  let offset = 0
  for (let page = 0; page < maxPages; page++) {
    const path = `/api/sessions?profile=${encodeURIComponent(backendProfile)}&limit=${pageSize}&offset=${offset}&order=recent`
    let data: any
    try {
      data = await ports.fetchJson(path)
    } catch {
      break
    }

    const rows: any[] = Array.isArray(data?.sessions)
      ? data.sessions
      : Array.isArray(data)
        ? data
        : []

    if (rows.length === 0) {
      break
    }

    for (const row of rows) {
      if (row && typeof row.id === 'string' && row.id && !row.ended_at && !seenIds.has(row.id)) {
        seenIds.add(row.id)
        const profile = typeof row.profile === 'string' && row.profile ? row.profile : backendProfile
        activeCandidates.push({ id: row.id, profile })
        if (activeCandidates.length >= maxActive) {
          break
        }
      }
    }

    if (activeCandidates.length >= maxActive) {
      break
    }

    const total = typeof data?.total === 'number' ? data.total : null
    offset += rows.length
    if (rows.length < pageSize || (total !== null && offset >= total)) {
      break
    }
  }

  if (activeCandidates.length === 0) {
    return []
  }

  const out = new Array<ContinuityLiveSession>(activeCandidates.length)
  let nextIdx = 0
  const worker = async () => {
    while (nextIdx < activeCandidates.length) {
      const idx = nextIdx++
      const candidate = activeCandidates[idx]
      let live: string | null = null
      try {
        live = await ports.readLive(backendProfile, candidate.profile, candidate.id)
      } catch {
        live = null
      }
      out[idx] = { profile: candidate.profile, sessionId: candidate.id, live }
    }
  }

  const workerCount = Math.min(concurrency, activeCandidates.length)
  await Promise.all(Array.from({ length: workerCount }, worker))

  return out
}

export function createOwnerGrantContinuityIssuer(ports: ContinuityIssuerPorts): ContinuityIssuer {
  // backend profile -> `<profile>\u0000<session id>` -> the latest observation (main-owned memory)
  const observed = new Map<string, Map<string, ContinuityObservation & { profile: string; sid: string }>>()
  const current = new Map<string, string>()
  const issued = new Set<string>()
  const generation = new Map<string, number>()
  const setTimer = ports.setTimer ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))
  const sleep = (ms: number) => new Promise<void>(resolve => setTimer(resolve, ms))

  async function watchOne(
    backendProfile: string,
    before: ContinuityObservation & { profile: string; sid: string },
    backend: string,
    gen: number
  ): Promise<ContinuityOutcome | null> {
    const deadline = ports.now() + CONTINUITY_WATCH_MS

    for (;;) {
      // Wait first: the relaunched backend is still starting when the spawn hook fires.
      await sleep(CONTINUITY_POLL_MS)

      // A newer relaunch of this profile supersedes this watch (its backend id is dead).
      if (generation.get(backendProfile) !== gen) {
        return null
      }

      let live: string | null = null

      try {
        const value = await ports.readLive(backendProfile, before.profile, before.sid)
        live = nonEmpty(value) ? value : null
      } catch {
        live = null
      }

      if (live) {
        if (live === before.claudeSid) {
          return null
        }

        const key = `${before.profile}\u0000${before.sid}\u0000${before.claudeSid}\u0000${live}`

        if (issued.has(key)) {
          return null
        }

        const outcome = await issueContinuityGrant(
          {
            backendProfile,
            profile: before.profile,
            hermesSessionId: before.sid,
            backend,
            oldClaudeSid: before.claudeSid,
            newClaudeSid: live,
            observed: { backend: before.backend, claudeSid: before.claudeSid, at: before.at }
          },
          ports
        )

        if (outcome.issued) {
          issued.add(key)
        }

        ports.log?.(
          `[owner-grant] continuity ${'reason' in outcome ? `refused (${outcome.reason})` : `issued ${outcome.grantId}`} for ${before.profile}:${before.sid}`
        )

        return outcome
      }

      if (ports.now() + CONTINUITY_POLL_MS > deadline) {
        return null
      }
    }
  }

  return {
    async observe(backendProfile, backend) {
      if (!nonEmpty(backendProfile) || !nonEmpty(backend)) {
        return
      }

      current.set(backendProfile, current.get(backendProfile) ?? backend)

      if (current.get(backendProfile) !== backend) {
        return
      }

      const sessions = await ports.listLiveSessions(backendProfile)

      // The backend was relaunched while we read: what we read may be either backend's. Drop it.
      if (current.get(backendProfile) !== backend) {
        return
      }

      const at = ports.now()
      const table = observed.get(backendProfile) ?? new Map()

      for (const s of sessions.slice(0, MAX_OBSERVED)) {
        if (!nonEmpty(s?.profile) || !nonEmpty(s?.sessionId) || !nonEmpty(s?.live)) {
          continue
        }

        const key = `${s.profile}\u0000${s.sessionId}`

        if (!table.has(key) && table.size >= MAX_OBSERVED) {
          table.delete(table.keys().next().value as string)
        }

        table.set(key, { backend, claudeSid: s.live, at, profile: s.profile, sid: s.sessionId })
      }

      observed.set(backendProfile, table)
    },

    async onBackendRelaunch(backendProfile, backend) {
      const gen = (generation.get(backendProfile) ?? 0) + 1
      generation.set(backendProfile, gen)
      current.set(backendProfile, backend)
      const now = ports.now()

      // Freeze the previous backend's recent observations; the new backend starts a fresh table.
      const before = [...(observed.get(backendProfile)?.values() ?? [])].filter(
        o => o.backend !== backend && now - o.at <= CONTINUITY_OBSERVATION_MAX_AGE_MS
      )

      observed.delete(backendProfile)
      const outcomes = await Promise.all(before.map(o => watchOne(backendProfile, o, backend, gen)))

      return outcomes.filter((o): o is ContinuityOutcome => o !== null)
    }
  }
}
