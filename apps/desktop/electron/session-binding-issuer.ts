/**
 * b10 H7b: launch-attestation issuer. Electron main ONLY.
 *
 * Spec: DESIGN-B10-SESSION-PROJECT-BINDING-2026-09-29.md §0, §3, §4, §5, §7.
 * - Long-poll consumer: GET <backend>/api/session-launches?since=<seq>&wait=2 with main's token.
 * - Error backoff: 1 -> 2 -> 5 -> 15 s. Stop on dispose.
 * - Signs hermes-launch-attestation/v1 for each launch.
 * - Bound verified record -> bound payload; unbound/needs_reconfirm -> null binding fields.
 * - Atomic write: temp wx 0600 + fsync + rename under <grants_dir>/session-attest/<claude_sid>/.
 * - Refresh attestation at TTL/2 (15 min) while in feed; stop when disappeared.
 * - Every issue re-reads the verified store at sign time (never a cached record); a /compress child
 *   with no record of its own carries its nearest recorded lineage ancestor's bound binding.
 * - revoke(profile, hermes_session_id): re-issues with nulls for live launches of that session.
 * - onBindingChanged(profile, hermes_session_id): re-issues matching live launches at once.
 * - Each live launch has a generation; a refresh that awaited past a newer issue is discarded.
 * - isAttestationLive(profile, hermes_session_id): true if live non-expired bound attestation exists.
 * - Anchor gate failure (anchor_missing/anchor_mismatch) -> idles ("signing off"), never throws.
 */

import { randomBytes as nodeRandomBytes } from 'node:crypto'
import nodeFs from 'node:fs'
import path from 'node:path'

import {
  ATTESTATION_AUDIENCE,
  ATTESTATION_FORMAT,
  defaultOwnerGrantsDir,
  OwnerKeyError,
  type OwnerDomainEnvelope,
  type OwnerKeyStore
} from './owner-grant-key'
import { encodePayload } from './owner-grant-sign'
import { isValidPathComponent, type SessionBindingRecord, type SessionBindingStore } from './session-binding-store'

export const ATTEST_MAX_TTL_MS = 30 * 60 * 1000 // 30 minutes in ms
export const ATTEST_REFRESH_INTERVAL_MS = 15 * 60 * 1000 // 15 minutes in ms (TTL / 2)
export const ISSUER_BACKOFF_MS = [1_000, 2_000, 5_000, 15_000] as const

export interface LaunchFeedEntry {
  hermes_session_id: string
  claude_session_id: string
  profile: string
  launch_seq: number
  hermes_lineage?: string[]
  recorded_at?: number
  backend?: string
}

export interface LaunchAttestationPayload {
  v: 1
  aud: readonly string[]
  owner_uid: number
  profile: string
  backend: string
  hermes_session_id: string
  claude_session_id: string
  launch_seq: number
  project_root: string | null
  repo_common_root: string | null
  binding_nonce: string | null
  binding_seq: number | null
  repo_remote: string | null
  hermes_lineage: string[]
  issued_at: number
  expires_at: number
}

export interface SessionAttestationIssuerDeps {
  store?: SessionBindingStore
  bindingStore?: SessionBindingStore
  keyStore: Pick<OwnerKeyStore, 'assertMaySign' | 'signDomain' | 'publicInfo' | 'anchorState'>
  grantsDir?: string
  ownerUid?: number
  backend?: string
  getBackendId?: (profile: string) => string | null
  fetch?: (url: string, init?: any) => Promise<any>
  fetchJson?: (urlOrPath: string, options?: any) => Promise<any>
  requestLaunches?: (since: number, wait: number) => Promise<{ seq: number; launches: unknown[] }>
  now?: () => number
  setTimeout?: typeof setTimeout
  clearTimeout?: typeof clearTimeout
  fs?: typeof nodeFs
  randomBytes?: (n: number) => Buffer
  log?: (message: string, meta?: Record<string, unknown>) => void
  ttlMs?: number
  refreshIntervalMs?: number
  autoStart?: boolean
}

export interface LiveLaunchRecord {
  launch: LaunchFeedEntry
  claude_session_id: string
  profile: string
  hermes_session_id: string
  backend: string
  launch_seq: number
  binding_nonce: string | null
  issued_at: number
  expires_at: number
  filePath?: string
  refreshTimer?: any
  isBound: boolean
  /** The hermes session whose binding this attestation carries: the launch's own id, or a lineage
   *  ancestor after /compress; null when unbound. */
  binding_source: string | null
  /** Bumped on every (re-)issue attempt: revoke, onBindingChanged, a compression / newer launch
   *  update, a refresh. A refresh that awaited past a bump is stale and drops its work. */
  generation: number
}

export interface SessionAttestationIssuer {
  start(): void
  stop(): void
  dispose(): void
  pollOnce(): Promise<void>
  revoke(profile: string, hermes_session_id: string): Promise<void>
  /** Re-issue matching live launches right after an owner bind / re-bind / unbind of that session. */
  onBindingChanged(profile: string, hermes_session_id: string): Promise<void>
  isAttestationLive(profile: string, hermes_session_id: string): boolean
  getLiveLaunches(): ReadonlyMap<string, LiveLaunchRecord>
}

export function writeAttestationFile(
  grantsDir: string,
  claudeSessionId: string,
  launchSeq: number,
  issuedAt: number,
  envelope: OwnerDomainEnvelope,
  fs: typeof nodeFs = nodeFs,
  random: (n: number) => Buffer = nodeRandomBytes
): { ok: true; path: string } | { ok: false; reason: string } {
  if (!isValidPathComponent(claudeSessionId)) {
    return { ok: false, reason: 'bad_input' }
  }

  try {
    fs.mkdirSync(grantsDir, { recursive: true, mode: 0o700 })
  } catch (err: any) {
    if (err?.code !== 'EEXIST') {
      return { ok: false, reason: 'io_error' }
    }
  }

  let realGrantsDir: string
  try {
    realGrantsDir = fs.realpathSync(grantsDir)
  } catch {
    return { ok: false, reason: 'bad_target' }
  }

  try {
    const grantsSt = fs.lstatSync(realGrantsDir)
    if (!grantsSt.isDirectory() || grantsSt.isSymbolicLink()) {
      return { ok: false, reason: grantsSt.isSymbolicLink() ? 'symlink_refused' : 'bad_target' }
    }
  } catch {
    return { ok: false, reason: 'bad_target' }
  }

  const attestBaseDir = path.join(realGrantsDir, 'session-attest')
  try {
    const st = fs.lstatSync(attestBaseDir)
    if (st.isSymbolicLink()) {
      return { ok: false, reason: 'symlink_refused' }
    }
    if (!st.isDirectory()) {
      return { ok: false, reason: 'bad_target' }
    }
  } catch (err: any) {
    if (err?.code === 'ENOENT') {
      fs.mkdirSync(attestBaseDir, { recursive: true, mode: 0o700 })
      const st = fs.lstatSync(attestBaseDir)
      if (st.isSymbolicLink()) {
        return { ok: false, reason: 'symlink_refused' }
      }
      if (!st.isDirectory()) {
        return { ok: false, reason: 'bad_target' }
      }
    } else {
      return { ok: false, reason: 'io_error' }
    }
  }

  const sessionDir = path.join(attestBaseDir, claudeSessionId)
  try {
    const st = fs.lstatSync(sessionDir)
    if (st.isSymbolicLink()) {
      return { ok: false, reason: 'symlink_refused' }
    }
    if (!st.isDirectory()) {
      return { ok: false, reason: 'bad_target' }
    }
  } catch (err: any) {
    if (err?.code === 'ENOENT') {
      fs.mkdirSync(sessionDir, { recursive: true, mode: 0o700 })
      const st = fs.lstatSync(sessionDir)
      if (st.isSymbolicLink()) {
        return { ok: false, reason: 'symlink_refused' }
      }
      if (!st.isDirectory()) {
        return { ok: false, reason: 'bad_target' }
      }
    } else {
      return { ok: false, reason: 'io_error' }
    }
  }

  const finalPath = path.join(sessionDir, `${issuedAt}-${launchSeq}.json`)
  try {
    const st = fs.lstatSync(finalPath)
    if (st.isSymbolicLink()) {
      return { ok: false, reason: 'symlink_refused' }
    }
  } catch (err: any) {
    if (err?.code !== 'ENOENT') {
      return { ok: false, reason: 'io_error' }
    }
  }

  const text = JSON.stringify({
    format: envelope.format,
    kid: envelope.kid,
    payload: envelope.payload,
    sig: envelope.sig
  })

  const tmpName = `.${issuedAt}.${process.pid}.${random(6).toString('hex')}.tmp`
  const tmpPath = path.join(sessionDir, tmpName)

  let fd: number
  try {
    fd = fs.openSync(tmpPath, 'wx', 0o600)
  } catch {
    return { ok: false, reason: 'io_error' }
  }

  try {
    fs.writeSync(fd, text)
    fs.fsyncSync(fd)
  } finally {
    fs.closeSync(fd)
  }

  try {
    fs.renameSync(tmpPath, finalPath)
  } catch {
    try {
      fs.unlinkSync(tmpPath)
    } catch {
      // ignore
    }
    return { ok: false, reason: 'io_error' }
  }

  return { ok: true, path: finalPath }
}

export function createSessionAttestationIssuer(deps: SessionAttestationIssuerDeps): SessionAttestationIssuer {
  const store = deps.bindingStore ?? deps.store
  const keyStore = deps.keyStore
  const grantsDir = deps.grantsDir ?? defaultOwnerGrantsDir()
  const ownerUid = deps.ownerUid ?? (typeof process.getuid === 'function' ? process.getuid() : 501)
  const fs = deps.fs ?? nodeFs
  const random = deps.randomBytes ?? nodeRandomBytes
  const nowFn = deps.now ?? Date.now
  const setTimeoutFn = deps.setTimeout ?? setTimeout
  const clearTimeoutFn = deps.clearTimeout ?? clearTimeout
  const ttlMs = deps.ttlMs ?? ATTEST_MAX_TTL_MS
  const refreshIntervalMs = deps.refreshIntervalMs ?? ATTEST_REFRESH_INTERVAL_MS

  const liveLaunches = new Map<string, LiveLaunchRecord>()
  const pendingSleepTimers = new Set<any>()

  let currentSeq = 0
  let consecutiveErrors = 0
  let running = false
  let disposed = false

  async function fetchLaunches(since: number, wait: number): Promise<{ seq: number; launches: LaunchFeedEntry[] }> {
    let result: any
    if (deps.requestLaunches) {
      result = await deps.requestLaunches(since, wait)
    } else if (deps.fetchJson) {
      result = await deps.fetchJson(`/api/session-launches?since=${since}&wait=${wait}`)
    } else if (deps.fetch) {
      const res = await deps.fetch(`/api/session-launches?since=${since}&wait=${wait}`)
      result = typeof res?.json === 'function' ? await res.json() : res
    } else {
      throw new Error('No fetch or fetchJson implementation provided')
    }

    if (!result || typeof result !== 'object') {
      throw new Error('Invalid response from session-launches endpoint')
    }

    const seq = typeof result.seq === 'number' ? result.seq : since
    const launches = Array.isArray(result.launches) ? result.launches : []

    return { seq, launches }
  }

  function validateFeedEntry(entry: unknown): LaunchFeedEntry | null {
    if (!entry || typeof entry !== 'object') {
      return null
    }

    const rec = entry as Record<string, unknown>
    const csid = rec.claude_session_id
    const hsid = rec.hermes_session_id
    const profile = rec.profile
    const launchSeq = rec.launch_seq

    if (!isValidPathComponent(csid) || !isValidPathComponent(hsid) || !isValidPathComponent(profile)) {
      return null
    }

    if (typeof launchSeq !== 'number' || !Number.isSafeInteger(launchSeq) || launchSeq < 0) {
      return null
    }

    let lineage: string[] = []
    if (Array.isArray(rec.hermes_lineage)) {
      lineage = rec.hermes_lineage
        .filter((x): x is string => typeof x === 'string' && x.length > 0 && !x.includes('\0'))
        .slice(-16)
    }

    return {
      claude_session_id: csid,
      hermes_session_id: hsid,
      profile,
      launch_seq: launchSeq,
      hermes_lineage: lineage,
      recorded_at: typeof rec.recorded_at === 'number' ? rec.recorded_at : undefined,
      backend: typeof rec.backend === 'string' ? rec.backend : undefined
    }
  }

  function canSign(): boolean {
    try {
      keyStore.assertMaySign?.([])
      return true
    } catch (err) {
      if (
        err instanceof OwnerKeyError &&
        (err.code === 'anchor_missing' || err.code === 'anchor_mismatch' || err.code === 'no_key')
      ) {
        deps.log?.(`[session-binding-issuer] signing off: ${err.message}`)
        return false
      }
      deps.log?.(`[session-binding-issuer] assertMaySign error: ${err}`)
      return false
    }
  }

  function resolveBackendId(entry: LaunchFeedEntry): string {
    if (entry.backend && typeof entry.backend === 'string') {
      return entry.backend
    }
    if (deps.getBackendId) {
      const bid = deps.getBackendId(entry.profile)
      if (bid) return bid
    }
    if (typeof deps.backend === 'string' && deps.backend) {
      return deps.backend
    }
    return 'spawn-1'
  }

  function isBoundVerified(binding: SessionBindingRecord | null | undefined): binding is SessionBindingRecord {
    return Boolean(binding && binding.state === 'bound' && binding.verified)
  }

  /**
   * The binding a launch carries, read from the verified store NOW (never from a cached live
   * record, so an unbind, a re-bind or a first bind is picked up by the next issue):
   * - an explicit record for the launch's own hermes_session_id (bound or unbound) always wins;
   * - with no own record (the child of a `/compress`), the launch's `hermes_lineage` is walked
   *   newest -> oldest and the NEAREST ancestor that has a record decides: a bound + verified one
   *   is carried forward with its own binding_nonce / binding_seq; an unbound or needs_reconfirm
   *   one stops the walk, so an owner's unbind of the parent is never bypassed by an older bound
   *   root further up the chain.
   *
   * Trust: `hermes_lineage` comes from the backend's launch feed (the backend's in-memory runtime
   * lineage, read with main's dashboard token, which agents never hold), never from state.db.
   * That is the same trust §4 gives the Claude sid: a compromised backend could already report any
   * hermes_session_id for a Claude sid, so naming an ancestor in the lineage gives it no new power.
   */
  function resolveBinding(entry: LaunchFeedEntry): SessionBindingRecord | null {
    if (!store) {
      return null
    }

    const own = store.get(entry.profile, entry.hermes_session_id)

    if (own) {
      return isBoundVerified(own) ? own : null
    }

    const lineage = entry.hermes_lineage ?? []

    for (let i = lineage.length - 1; i >= 0; i--) {
      const ancestorId = lineage[i]

      if (ancestorId === entry.hermes_session_id) {
        continue
      }

      const ancestor = store.get(entry.profile, ancestorId)

      if (!ancestor) {
        continue
      }

      return isBoundVerified(ancestor) ? ancestor : null
    }

    return null
  }

  function armRefresh(record: LiveLaunchRecord): void {
    if (record.refreshTimer) {
      clearTimeoutFn(record.refreshTimer)
    }

    record.refreshTimer = setTimeoutFn(() => {
      void refreshLaunch(record.claude_session_id)
    }, refreshIntervalMs)
  }

  /**
   * Sign and write one attestation for `entry`. Synchronous from the store read to the live-record
   * swap, so nothing can interleave between "which binding" and "what is signed". Every attempt
   * bumps the launch's generation, so a refresh that captured the record before an await sees it
   * is stale and drops its work. `forceNull` signs the explicit revocation (null binding fields).
   */
  function issueAttestation(entry: LaunchFeedEntry, issuedAt: number, options: { forceNull?: boolean } = {}): boolean {
    const existing = liveLaunches.get(entry.claude_session_id)
    const generation = (existing?.generation ?? 0) + 1

    if (existing) {
      existing.generation = generation
    }

    const failed = (): boolean => {
      // The previous file is still the newest on disk; keep retrying on the refresh cadence.
      if (existing && !disposed) {
        armRefresh(existing)
      }

      return false
    }

    if (!canSign()) {
      return failed()
    }

    // Re-read the verified store at sign time; anything but bound + verified signs nulls.
    const binding = options.forceNull ? null : resolveBinding(entry)
    const isBound = isBoundVerified(binding)
    const backend = resolveBackendId(entry)

    const payload: LaunchAttestationPayload = {
      v: 1,
      aud: [...ATTESTATION_AUDIENCE],
      owner_uid: ownerUid,
      profile: entry.profile,
      backend,
      hermes_session_id: entry.hermes_session_id,
      claude_session_id: entry.claude_session_id,
      launch_seq: entry.launch_seq,
      project_root: isBound ? binding!.project_root : null,
      repo_common_root: isBound ? (binding!.repo_common_root ?? null) : null,
      binding_nonce: isBound ? binding!.binding_nonce : null,
      binding_seq: isBound ? (binding!.seq ?? null) : null,
      repo_remote: isBound ? (binding!.repo_remote ?? null) : null,
      hermes_lineage: entry.hermes_lineage ?? [],
      issued_at: issuedAt,
      expires_at: issuedAt + ttlMs
    }

    let envelope: OwnerDomainEnvelope
    try {
      const payloadBytes = encodePayload(payload as unknown as Record<string, unknown>)
      envelope = keyStore.signDomain(ATTESTATION_FORMAT, payloadBytes)
    } catch (err) {
      if (
        err instanceof OwnerKeyError &&
        (err.code === 'anchor_missing' || err.code === 'anchor_mismatch' || err.code === 'no_key')
      ) {
        deps.log?.(`[session-binding-issuer] signing off during signDomain: ${err.message}`)
        return failed()
      }
      deps.log?.(`[session-binding-issuer] signDomain error: ${err}`)
      return failed()
    }

    const written = writeAttestationFile(grantsDir, entry.claude_session_id, entry.launch_seq, issuedAt, envelope, fs, random)
    if ('reason' in written) {
      deps.log?.(`[session-binding-issuer] writeAttestationFile failed: ${written.reason}`)
      return failed()
    }

    if (existing?.refreshTimer) {
      clearTimeoutFn(existing.refreshTimer)
      existing.refreshTimer = undefined
    }

    const record: LiveLaunchRecord = {
      launch: entry,
      claude_session_id: entry.claude_session_id,
      profile: entry.profile,
      hermes_session_id: entry.hermes_session_id,
      backend,
      launch_seq: entry.launch_seq,
      binding_nonce: payload.binding_nonce,
      binding_source: isBound ? binding!.hermes_session_id : null,
      issued_at: issuedAt,
      expires_at: payload.expires_at,
      filePath: written.path,
      isBound,
      generation
    }

    liveLaunches.set(entry.claude_session_id, record)

    if (!disposed) {
      armRefresh(record)
    }

    return true
  }

  function newestFeedEntryFor(launches: unknown[], claudeSessionId: string): LaunchFeedEntry | null {
    let newest: LaunchFeedEntry | null = null

    for (const raw of launches) {
      const entry = validateFeedEntry(raw)

      if (entry && entry.claude_session_id === claudeSessionId && (!newest || entry.launch_seq >= newest.launch_seq)) {
        newest = entry
      }
    }

    return newest
  }

  async function refreshLaunch(claudeSessionId: string): Promise<void> {
    if (disposed) return
    const record = liveLaunches.get(claudeSessionId)
    if (!record) return
    const generation = record.generation

    let snapshot: { seq: number; launches: unknown[] } | null = null
    try {
      snapshot = await fetchLaunches(0, 0)
    } catch {
      // On snapshot fetch error, do not drop blindly
      snapshot = null
    }

    // Anything that happened during the await (an unbind, a re-bind, a compression update, a
    // newer launch) re-issued this launch and bumped its generation: this refresh is stale.
    const current = liveLaunches.get(claudeSessionId)
    if (disposed || current !== record || current.generation !== generation) {
      deps.log?.(`[session-binding-issuer] stale refresh discarded: ${claudeSessionId}`)
      return
    }

    let launch = record.launch

    if (snapshot) {
      const active = newestFeedEntryFor(snapshot.launches, claudeSessionId)

      if (!active) {
        if (record.refreshTimer) {
          clearTimeoutFn(record.refreshTimer)
          record.refreshTimer = undefined
        }
        liveLaunches.delete(claudeSessionId)
        deps.log?.(`[session-binding-issuer] stopped refresh, launch disappeared from feed: ${claudeSessionId}`)
        return
      }

      // The feed's own view wins over the cached launch (e.g. /compress moved it to a child id).
      if (active.launch_seq >= record.launch.launch_seq) {
        launch = active
      }
    }

    issueAttestation(launch, nowFn())
  }

  async function reconcileActiveLaunches(): Promise<void> {
    try {
      const snapshot = await fetchLaunches(0, 0)
      const activeCids = new Set(
        snapshot.launches
          .map(l => (typeof l?.claude_session_id === 'string' ? l.claude_session_id : null))
          .filter((cid): cid is string => cid !== null)
      )

      for (const [cid, record] of Array.from(liveLaunches.entries())) {
        if (!activeCids.has(cid)) {
          if (record.refreshTimer) {
            clearTimeoutFn(record.refreshTimer)
            record.refreshTimer = undefined
          }
          liveLaunches.delete(cid)
          deps.log?.(`[session-binding-issuer] launch retired from feed: ${cid}`)
        }
      }
    } catch (err) {
      deps.log?.(`[session-binding-issuer] reconcile active launches failed: ${err}`)
    }
  }

  async function processLaunch(raw: unknown): Promise<void> {
    const entry = validateFeedEntry(raw)
    if (!entry) {
      deps.log?.('[session-binding-issuer] skipping malformed launch entry')
      return
    }

    issueAttestation(entry, nowFn())
  }

  async function pollOnce(): Promise<void> {
    if (disposed) return

    const feed = await fetchLaunches(currentSeq, 2)
    if (disposed) return

    const prevSeq = currentSeq
    if (typeof feed.seq === 'number') {
      currentSeq = feed.seq
    }

    if (feed.launches.length > 0) {
      for (const rawLaunch of feed.launches) {
        await processLaunch(rawLaunch)
      }
    } else if (feed.seq > prevSeq) {
      await reconcileActiveLaunches()
    }
  }

  function sleep(ms: number): Promise<void> {
    return new Promise(resolve => {
      if (disposed || !running) {
        resolve()
        return
      }
      const timer = setTimeoutFn(() => {
        pendingSleepTimers.delete(timer)
        resolve()
      }, ms)
      pendingSleepTimers.add(timer)
    })
  }

  async function runLoop(): Promise<void> {
    running = true
    while (running && !disposed) {
      try {
        await pollOnce()
        consecutiveErrors = 0
      } catch (err) {
        if (disposed || !running) break
        const delay = ISSUER_BACKOFF_MS[Math.min(consecutiveErrors, ISSUER_BACKOFF_MS.length - 1)]
        consecutiveErrors++
        deps.log?.(`[session-binding-issuer] poll error (err count: ${consecutiveErrors}), backing off ${delay}ms: ${err}`)
        await sleep(delay)
      }
    }
  }

  /** Live launches whose attestation depends on (profile, hermes_session_id): its own launches,
   *  launches carrying its binding, and /compress descendants that name it in their lineage. */
  function affectedLaunches(profile: string, hermes_session_id: string): LiveLaunchRecord[] {
    return Array.from(liveLaunches.values()).filter(
      l =>
        l.profile === profile &&
        (l.hermes_session_id === hermes_session_id ||
          l.binding_source === hermes_session_id ||
          (l.launch.hermes_lineage ?? []).includes(hermes_session_id))
    )
  }

  /**
   * The owner changed (profile, hermes_session_id)'s binding (bind, re-bind, or unbind): re-issue
   * every matching live launch now, from the store as it is now, instead of waiting for the refresh.
   */
  async function onBindingChanged(profile: string, hermes_session_id: string): Promise<void> {
    if (disposed || !isValidPathComponent(profile) || !isValidPathComponent(hermes_session_id)) {
      return
    }

    for (const record of affectedLaunches(profile, hermes_session_id)) {
      if (liveLaunches.get(record.claude_session_id) !== record) {
        continue
      }

      if (issueAttestation(record.launch, nowFn())) {
        deps.log?.(`[session-binding-issuer] re-issued launch attestation after a binding change: ${record.claude_session_id}`)
      }
    }
  }

  async function revoke(profile: string, hermes_session_id: string): Promise<void> {
    if (disposed || !isValidPathComponent(profile) || !isValidPathComponent(hermes_session_id)) {
      return
    }

    for (const record of affectedLaunches(profile, hermes_session_id)) {
      if (liveLaunches.get(record.claude_session_id) !== record) {
        continue
      }

      // The revoked session's own launches and anything carrying its binding sign explicit nulls;
      // other descendants re-resolve (the store's unbound record stops their lineage walk).
      const forceNull = record.hermes_session_id === hermes_session_id || record.binding_source === hermes_session_id

      if (issueAttestation(record.launch, nowFn(), { forceNull })) {
        deps.log?.(`[session-binding-issuer] revoked launch attestation for ${record.claude_session_id}`)
      }
    }
  }

  function isAttestationLive(profile: string, hermes_session_id: string): boolean {
    if (!isValidPathComponent(profile) || !isValidPathComponent(hermes_session_id)) {
      return false
    }

    const now = nowFn()
    for (const record of liveLaunches.values()) {
      if (
        record.profile === profile &&
        (record.hermes_session_id === hermes_session_id || record.binding_source === hermes_session_id)
      ) {
        if (record.binding_nonce !== null && record.binding_nonce !== undefined) {
          if (now <= record.expires_at) {
            return true
          }
        }
      }
    }

    return false
  }

  function start(): void {
    if (running || disposed) return
    void runLoop()
  }

  function stop(): void {
    running = false
    disposed = true
    for (const timer of pendingSleepTimers) {
      clearTimeoutFn(timer)
    }
    pendingSleepTimers.clear()

    for (const record of liveLaunches.values()) {
      if (record.refreshTimer) {
        clearTimeoutFn(record.refreshTimer)
        record.refreshTimer = undefined
      }
    }
  }

  function dispose(): void {
    stop()
    liveLaunches.clear()
  }

  if (deps.autoStart) {
    start()
  }

  return {
    start,
    stop,
    dispose,
    pollOnce,
    revoke,
    onBindingChanged,
    isAttestationLive,
    getLiveLaunches: () => liveLaunches
  }
}
