/**
 * b10 H8: session-binding IPC handlers and main-side path/git probing.
 *
 * Exposes `hermes:session-binding:{set,clear,status}` over the H7a SessionBindingStore.
 * Main trusts only {profile, hermes_session_id, path} from the renderer:
 * - App-chrome main-frame sender check;
 * - Confirm rate gate (one dialog at a time, 3 s cooldown after cancel, max 10/min);
 * - Main-side realpath + isDirectory verification;
 * - Main-side git probing: toplevel (`git rev-parse --show-toplevel`),
 *   common repo root (`git rev-parse --git-common-dir`), optional remote origin URL;
 * - Re-binding a session with an active live-attested build requires native confirmRebind;
 * - Renderer-supplied project_root or sequence fields are completely ignored;
 * - Clear records an unbound state with seq+1;
 * - Status reports current record state including needs_reconfirm.
 */

import { execFile } from 'node:child_process'
import nodeFs from 'node:fs'
import path from 'node:path'

import { createConfirmRateGate, type RateRefusal } from './owner-forward-confirm'
import {
  isValidPathComponent,
  type SessionBindingOutcome,
  type SessionBindingRecord,
  type SessionBindingRefusal,
  type SessionBindingStore
} from './session-binding-store'

export interface RebindConfirmDetails {
  profile: string
  hermes_session_id: string
  currentProjectRoot: string | null
  /** null = unbind */
  newProjectRoot: string | null
  event?: unknown
}

export interface ProbedWorkspace {
  ok: true
  project_root: string
  repo_common_root: string | null
  repo_remote: string | null
}

export interface ProbedWorkspaceRefusal {
  ok: false
  reason: 'not_found' | 'not_a_directory' | 'symlink_refused' | 'not_git' | 'bad_target' | 'bad_input'
  error?: unknown
}

export type ProbeWorkspaceResult = ProbedWorkspace | ProbedWorkspaceRefusal

export function runGit(
  gitBin: string,
  args: readonly string[],
  cwd: string,
  timeoutMs = 10_000
): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      gitBin,
      args,
      {
        cwd,
        windowsHide: true,
        timeout: timeoutMs,
        maxBuffer: 8 * 1024 * 1024,
        env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' }
      },
      (err, stdout, stderr) => {
        if (err) {
          ;(err as any).stderr = String(stderr || '')
          reject(err)
          return
        }
        resolve(String(stdout || ''))
      }
    )
  })
}

export async function probeWorkspace(
  requestedPath: string,
  options: { fs?: typeof nodeFs; gitBin?: string } = {}
): Promise<ProbeWorkspaceResult> {
  const fsImpl = options.fs ?? nodeFs
  const gitBin = options.gitBin ?? 'git'

  if (typeof requestedPath !== 'string' || !requestedPath.trim() || requestedPath.includes('\0')) {
    return { ok: false, reason: 'bad_input' }
  }

  const trimmed = requestedPath.trim()

  let lstat: nodeFs.Stats
  try {
    lstat = fsImpl.lstatSync(trimmed)
  } catch (err: any) {
    if (err?.code === 'ENOENT') {
      return { ok: false, reason: 'not_found' }
    }
    return { ok: false, reason: 'bad_target', error: err }
  }

  let realPath: string
  try {
    realPath = fsImpl.realpathSync(trimmed)
  } catch (err) {
    return { ok: false, reason: 'symlink_refused', error: err }
  }

  try {
    const stat = fsImpl.statSync(realPath)
    if (!stat.isDirectory()) {
      return { ok: false, reason: 'not_a_directory' }
    }
  } catch (err) {
    return { ok: false, reason: 'bad_target', error: err }
  }

  let toplevelOut: string
  try {
    toplevelOut = await runGit(gitBin, ['rev-parse', '--show-toplevel'], realPath)
  } catch (err) {
    return { ok: false, reason: 'not_git', error: err }
  }

  const rawToplevel = toplevelOut.trim()
  if (!rawToplevel) {
    return { ok: false, reason: 'not_git' }
  }

  let project_root: string
  try {
    project_root = fsImpl.realpathSync(rawToplevel)
  } catch {
    project_root = path.posix.normalize(rawToplevel)
  }

  let repo_common_root: string | null = null
  try {
    const commonDirOut = (await runGit(gitBin, ['rev-parse', '--git-common-dir'], realPath)).trim()
    if (commonDirOut) {
      const resolvedCommon = path.resolve(realPath, commonDirOut)
      let commonReal: string
      try {
        commonReal = fsImpl.existsSync(resolvedCommon) ? fsImpl.realpathSync(resolvedCommon) : resolvedCommon
      } catch {
        commonReal = resolvedCommon
      }
      if (path.basename(commonReal) === '.git') {
        const parentDir = path.dirname(commonReal)
        try {
          repo_common_root = fsImpl.realpathSync(parentDir)
        } catch {
          repo_common_root = parentDir
        }
      } else {
        repo_common_root = commonReal
      }
    }
  } catch {
    repo_common_root = null
  }

  let repo_remote: string | null = null
  try {
    const remoteOut = (await runGit(gitBin, ['remote', 'get-url', 'origin'], realPath)).trim()
    if (remoteOut) {
      repo_remote = remoteOut
    }
  } catch {
    repo_remote = null
  }

  return {
    ok: true,
    project_root,
    repo_common_root,
    repo_remote
  }
}

export interface SessionBindingIpcDeps {
  isTrustedSender: (event: unknown) => boolean
  store: SessionBindingStore
  probeWorkspace?: (path: string) => Promise<ProbeWorkspaceResult>
  confirmRebind?: (details: RebindConfirmDetails) => Promise<boolean>
  isAttestationLive?: (profile: string, hermes_session_id: string) => boolean
  now?: () => number
  log?: (message: string) => void
}

export interface SessionBindingStatusRecord {
  ok: true
  state: 'bound' | 'unbound' | 'needs_reconfirm'
  profile: string
  hermes_session_id: string
  seq: number
  binding_nonce: string | null
  bound_at: number | null
  project_root: string | null
  repo_common_root: string | null
  repo_remote: string | null
  project_id: string | null
  carried_from: string | null
  verified: boolean
  record: SessionBindingRecord | null
}

export type SessionBindingIpcResult =
  | SessionBindingOutcome
  | SessionBindingStatusRecord
  | { ok: false; reason: SessionBindingRefusal | RateRefusal | 'untrusted_sender' | 'cancelled' | 'not_git' | 'not_a_directory' | 'not_found'; error?: unknown }

export interface SessionBindingIpcHandlers {
  set: (event: unknown, input: unknown) => Promise<SessionBindingIpcResult>
  clear: (event: unknown, input: unknown) => Promise<SessionBindingIpcResult>
  status: (event: unknown, input: unknown) => Promise<SessionBindingIpcResult>
  handleSet: (event: unknown, input: unknown) => Promise<SessionBindingIpcResult>
  handleClear: (event: unknown, input: unknown) => Promise<SessionBindingIpcResult>
  handleStatus: (event: unknown, input: unknown) => Promise<SessionBindingIpcResult>
}

export function createSessionBindingIpcHandlers(deps: SessionBindingIpcDeps): SessionBindingIpcHandlers {
  const nowFn = deps.now ?? Date.now
  const gate = createConfirmRateGate(nowFn)
  const probeFn = deps.probeWorkspace ?? probeWorkspace
  const isAttestationLiveFn = deps.isAttestationLive ?? (() => false)

  async function handleSet(event: unknown, input: unknown): Promise<SessionBindingIpcResult> {
    if (!deps.isTrustedSender(event)) {
      deps.log?.('[session-binding] set refused: untrusted sender')
      return { ok: false, reason: 'untrusted_sender' }
    }

    const slot = gate.tryOpen()
    if ('reason' in slot) {
      return { ok: false, reason: slot.reason }
    }

    let cancelled = false

    try {
      const params = input as Record<string, unknown> | null | undefined
      const profile = params?.profile
      const hermes_session_id = params?.hermes_session_id
      const requestedPath = params?.path

      if (
        !isValidPathComponent(profile) ||
        !isValidPathComponent(hermes_session_id) ||
        typeof requestedPath !== 'string' ||
        !requestedPath.trim() ||
        requestedPath.includes('\0')
      ) {
        return { ok: false, reason: 'bad_input' }
      }

      const probed = await probeFn(requestedPath)
      if ('reason' in probed) {
        return { ok: false, reason: probed.reason }
      }

      const existing = deps.store.get(profile, hermes_session_id)
      const isLive = isAttestationLiveFn(profile, hermes_session_id)

      if (existing && existing.state === 'bound' && isLive) {
        if (deps.confirmRebind) {
          const confirmed = await deps.confirmRebind({
            profile,
            hermes_session_id,
            currentProjectRoot: existing.project_root,
            newProjectRoot: probed.project_root,
            event
          })
          if (!confirmed) {
            cancelled = true
            return { ok: false, reason: 'cancelled' }
          }
        }
      }

      try {
        const outcome = deps.store.bind({
          profile,
          hermes_session_id,
          project_root: probed.project_root,
          repo_common_root: probed.repo_common_root,
          repo_remote: probed.repo_remote
        })
        return outcome
      } catch (err) {
        deps.log?.(`[session-binding] store bind failed: ${err}`)
        return { ok: false, reason: 'signing_off', error: err }
      }
    } finally {
      gate.close(cancelled)
    }
  }

  async function handleClear(event: unknown, input: unknown): Promise<SessionBindingIpcResult> {
    if (!deps.isTrustedSender(event)) {
      deps.log?.('[session-binding] clear refused: untrusted sender')
      return { ok: false, reason: 'untrusted_sender' }
    }

    const slot = gate.tryOpen()
    if ('reason' in slot) {
      return { ok: false, reason: slot.reason }
    }

    let cancelled = false
    try {
      const params = input as Record<string, unknown> | null | undefined
      const profile = params?.profile
      const hermes_session_id = params?.hermes_session_id

      if (!isValidPathComponent(profile) || !isValidPathComponent(hermes_session_id)) {
        return { ok: false, reason: 'bad_input' }
      }

      const existing = deps.store.get(profile, hermes_session_id)
      if (existing && existing.state === 'bound' && isAttestationLiveFn(profile, hermes_session_id) && deps.confirmRebind) {
        // Owner O2: unbinding a session with a live build gets the same native confirm as a re-bind.
        const confirmed = await deps.confirmRebind({
          profile,
          hermes_session_id,
          currentProjectRoot: existing.project_root,
          newProjectRoot: null,
          event
        })
        if (!confirmed) {
          cancelled = true
          return { ok: false, reason: 'cancelled' }
        }
      }

      try {
        const outcome = deps.store.unbind(profile, hermes_session_id)
        return outcome
      } catch (err) {
        deps.log?.(`[session-binding] store unbind failed: ${err}`)
        return { ok: false, reason: 'signing_off', error: err }
      }
    } finally {
      gate.close(cancelled)
    }
  }

  async function handleStatus(event: unknown, input: unknown): Promise<SessionBindingIpcResult> {
    if (!deps.isTrustedSender(event)) {
      deps.log?.('[session-binding] status refused: untrusted sender')
      return { ok: false, reason: 'untrusted_sender' }
    }

    // Read-only: status never takes the confirm gate, so session switches can't spend the
    // budget the owner's own bind click needs.
    {
      const params = input as Record<string, unknown> | null | undefined
      const profile = params?.profile
      const hermes_session_id = params?.hermes_session_id

      if (!isValidPathComponent(profile) || !isValidPathComponent(hermes_session_id)) {
        return { ok: false, reason: 'bad_input' }
      }

      const record = deps.store.get(profile, hermes_session_id)
      if (record) {
        return {
          ok: true,
          state: record.state,
          profile: record.profile,
          hermes_session_id: record.hermes_session_id,
          seq: record.seq,
          binding_nonce: record.binding_nonce,
          bound_at: record.bound_at,
          project_root: record.project_root,
          repo_common_root: record.repo_common_root,
          repo_remote: record.repo_remote,
          project_id: record.project_id,
          carried_from: record.carried_from,
          verified: record.verified,
          record
        }
      }

      return {
        ok: true,
        state: 'unbound',
        profile,
        hermes_session_id,
        seq: 0,
        binding_nonce: null,
        bound_at: null,
        project_root: null,
        repo_common_root: null,
        repo_remote: null,
        project_id: null,
        carried_from: null,
        verified: false,
        record: null
      }
    }
  }

  return {
    set: handleSet,
    clear: handleClear,
    status: handleStatus,
    handleSet,
    handleClear,
    handleStatus
  }
}
