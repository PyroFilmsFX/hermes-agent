// Private session-token handoff to the local Desktop backend (P0 2026-09-26).
//
// The per-spawn session token authenticates /api/ws as the Desktop: every
// agent the backend runs is a same-user process, so the token must reach the
// backend without passing through anything those agents can read. The env
// var (HERMES_DASHBOARD_SESSION_TOKEN) is inherited by every child that is
// spawned without an explicit env, and `GET /` used to serve it to anyone on
// loopback. Instead, main writes it to a one-shot file:
//
//   ~/.hermes/desktop-local/<32 hex>/<16 hex>.token
//
// with the directory created 0700, the file created O_EXCL ('wx') at 0600,
// and `--session-token-file <path>` passed on argv. The backend
// (`hermes_cli/main_dashboard.py::_read_desktop_session_token_file`) validates
// the path, owner, and modes, reads the token, and unlinks the file before it
// imports the web server. Main removes the directory on child exit, on spawn
// failure, and at quit, whichever comes first.
//
// Runtimes that predate the flag keep the env handoff, which is the only thing
// they understand. `backendSupportsSessionTokenFile` decides which to use: it
// reads the runtime's own `serve` parser source, and when that is unreadable
// it runs `serve --help` and checks the output.

import path from 'node:path'

export const SESSION_TOKEN_FILE_FLAG = '--session-token-file'
export const SESSION_TOKEN_DIR_NAME = 'desktop-local'

export interface SessionTokenFileFs {
  mkdirSync: (target: string, options: { mode: number; recursive?: boolean }) => unknown
  chmodSync: (target: string, mode: number) => void
  openSync: (target: string, flags: string, mode: number) => number
  writeSync: (fd: number, data: string) => unknown
  closeSync: (fd: number) => void
  rmSync: (target: string, options: { force: boolean; recursive: boolean }) => void
}

export interface SessionTokenFileDeps {
  fs: SessionTokenFileFs
  homedir: () => string
  randomHex: (bytes: number) => string
  platform: string
}

export interface SessionTokenHandoff {
  dir: string
  file: string
}

/**
 * True when a runtime's `hermes_cli/subcommands/dashboard.py` registers the
 * flag. The quotes anchor the match, so `--ssh-session-token-file` (the SSH
 * spawn's older flag) never counts.
 */
export function sourceDeclaresSessionTokenFile(dashboardPySource: string | null | undefined): boolean {
  return /["']--session-token-file["']/.test(String(dashboardPySource || ''))
}

/** Same check against `hermes serve --help` output. */
export function helpDeclaresSessionTokenFile(helpText: string | null | undefined): boolean {
  return /(^|[\s,[])--session-token-file\b/m.test(String(helpText || ''))
}

export function sessionTokenRoot(homedir: string): string {
  return path.join(homedir, '.hermes', SESSION_TOKEN_DIR_NAME)
}

/**
 * Tracks every handoff directory this process created so each one is removed
 * exactly once: on child exit, on spawn failure, or at quit.
 */
export function createSessionTokenFiles(deps: SessionTokenFileDeps) {
  const live = new Set<string>()

  function remove(dir: string | null | undefined): void {
    if (!dir || !live.has(dir)) {
      return
    }

    live.delete(dir)

    try {
      deps.fs.rmSync(dir, { force: true, recursive: true })
    } catch {
      // Best effort. The backend already unlinked the file after reading it,
      // and a directory left behind holds no secret.
    }
  }

  function write(token: string): SessionTokenHandoff {
    if (!token) {
      throw new Error('refusing to write an empty session token')
    }

    const root = sessionTokenRoot(deps.homedir())
    deps.fs.mkdirSync(root, { mode: 0o700, recursive: true })

    if (deps.platform !== 'win32') {
      // mkdir's mode is masked by umask, and `recursive` leaves an existing
      // root's mode alone. The backend refuses anything looser than 0700.
      deps.fs.chmodSync(root, 0o700)
    }

    const dir = path.join(root, deps.randomHex(16))
    // Not recursive, so an existing directory throws EEXIST: a fresh name per
    // spawn, and never one somebody else prepared.
    deps.fs.mkdirSync(dir, { mode: 0o700 })
    live.add(dir)

    try {
      if (deps.platform !== 'win32') {
        deps.fs.chmodSync(dir, 0o700)
      }

      const file = path.join(dir, `${deps.randomHex(8)}.token`)
      // 'wx' = O_CREAT | O_EXCL | O_WRONLY. It fails on anything already
      // there, symlinks included.
      const fd = deps.fs.openSync(file, 'wx', 0o600)

      try {
        deps.fs.writeSync(fd, token)
      } finally {
        deps.fs.closeSync(fd)
      }

      if (deps.platform !== 'win32') {
        deps.fs.chmodSync(file, 0o600)
      }

      return { dir, file }
    } catch (error) {
      remove(dir)
      throw error
    }
  }

  function removeAll(): void {
    for (const dir of [...live]) {
      remove(dir)
    }
  }

  return { liveDirs: () => [...live], remove, removeAll, write }
}

export interface SessionTokenFileSupportCandidate {
  command?: string | null
  root?: string
  args?: string[]
  env?: Record<string, string>
  shell?: boolean
}

export interface SessionTokenFileSupportDeps {
  readFile: (target: string) => Promise<string>
  /** Run `serve --help` and resolve with its stdout. Reject on failure or timeout. */
  runHelp: (command: string, args: string[], candidate: SessionTokenFileSupportCandidate) => Promise<string>
  log: (message: string) => void
}

/**
 * Does this runtime's `serve` accept `--session-token-file`? Cached per
 * runtime. Anything uncertain answers false, which keeps the env handoff:
 * passing an unknown flag to an old runtime would crash its argparse.
 */
export function createSessionTokenFileSupportResolver(deps: SessionTokenFileSupportDeps) {
  const cache = new Map<string, Promise<boolean>>()

  return function backendSupportsSessionTokenFile(candidate: SessionTokenFileSupportCandidate): Promise<boolean> {
    if (!candidate?.command) {
      return Promise.resolve(false)
    }

    // A `.cmd`/`.bat` shim runs through a shell that re-splits argv, and a
    // path under a home directory with spaces would break. Keep the env
    // handoff there.
    if (candidate.shell) {
      return Promise.resolve(false)
    }

    const key = `${candidate.command}::${candidate.root || ''}`
    const cached = cache.get(key)

    if (cached) {
      return cached
    }

    const pending = (async () => {
      let supported: boolean | null = null

      if (candidate.root) {
        try {
          supported = sourceDeclaresSessionTokenFile(
            await deps.readFile(path.join(candidate.root, 'hermes_cli', 'subcommands', 'dashboard.py'))
          )
        } catch {
          supported = null
        }
      }

      if (supported === null) {
        try {
          const prefix = candidate.args && candidate.args[0] === '-m' ? candidate.args.slice(0, 2) : []
          supported = helpDeclaresSessionTokenFile(await deps.runHelp(candidate.command, [...prefix, 'serve', '--help'], candidate))
        } catch {
          // Evict so a cold-start timeout is re-probed on the next spawn.
          if (cache.get(key) === pending) {
            cache.delete(key)
          }

          supported = false
        }
      }

      deps.log(`[backend] session token handoff: ${supported ? 'private file' : 'environment (legacy runtime)'}`)

      return supported
    })()

    cache.set(key, pending)

    return pending
  }
}

/**
 * Build the spawn argv and env for one local backend. With the file handoff
 * the env has NO token at all (an inherited stale value is deleted too); the
 * env handoff is only for runtimes that predate the flag.
 */
export function applySessionTokenHandoff(
  args: string[],
  env: NodeJS.ProcessEnv,
  handoff: SessionTokenHandoff | null,
  token: string
): { args: string[]; env: NodeJS.ProcessEnv } {
  if (handoff) {
    const clean = { ...env }
    delete clean.HERMES_DASHBOARD_SESSION_TOKEN

    return { args: [...args, SESSION_TOKEN_FILE_FLAG, handoff.file], env: clean }
  }

  return { args: [...args], env: { ...env, HERMES_DASHBOARD_SESSION_TOKEN: token } }
}
