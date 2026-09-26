// dev-revision-stamp.ts — the revision a DEV run is actually executing (cntrl carry).
//
// The artifact stamp (./install-stamp INSTALL_STAMP) is baked at build time and is
// deliberately null on dev bundles; lifecycle decisions (installShape, update strategy,
// bundle skew/swap) gate on it and must keep seeing null in dev. This module is
// display-only: the boot log line, `hermes:version` commit/currentSha and the
// statusbar sha. It derives the sha from `git rev-parse HEAD` of the Hermes root
// (2 s timeout; it runs synchronously at boot), falls back to the backend-reported
// code_sha, and never reads a (stale) build/install-stamp.json.
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

export interface DevRevisionStamp {
  commit: string
  branch: string | null
  dirty: boolean
  source: 'git' | 'backend'
}

export interface FsReader {
  existsSync: (p: string) => boolean
  readFileSync: (p: string, encoding: any) => string
}

export interface LoadDevRevisionStampOptions {
  /** Hermes source root to run git in (e.g. HERMES_DESKTOP_HERMES_ROOT). */
  hermesRoot?: string
  /** App root; used when no hermesRoot is given. */
  appRoot?: string
  /** HERMES_HOME directory, for the backend's gateway_state.json. */
  hermesHome?: string
  /** Fallback code sha from the backend. */
  backendCodeSha?: string | null
  /** Injectable synchronous git runner for testing. */
  runGitSync?: (args: string[], cwd: string) => string | null
  /** Injectable filesystem reader. */
  fsImpl?: FsReader | typeof fs
}

function defaultRunGitSync(args: string[], cwd: string): string | null {
  try {
    const gitBin = process.platform === 'win32' ? 'git.exe' : 'git'

    return execFileSync(gitBin, args, {
      cwd,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      // Runs synchronously at startup: never let a wedged git (index lock, network fs) hang boot.
      timeout: 2000,
      windowsHide: true
    }).trim()
  } catch {
    return null
  }
}

function resolveBackendCodeSha(options: LoadDevRevisionStampOptions, fsImpl: FsReader | typeof fs): string | null {
  if (options.backendCodeSha && typeof options.backendCodeSha === 'string' && options.backendCodeSha.length >= 7) {
    return options.backendCodeSha
  }

  const hermesHome =
    options.hermesHome ||
    process.env.HERMES_HOME ||
    (process.platform === 'win32'
      ? path.join(process.env.LOCALAPPDATA || '', 'hermes')
      : path.join(process.env.HOME || '', '.hermes'))

  if (hermesHome) {
    const statePath = path.join(hermesHome, 'gateway_state.json')

    try {
      if (fsImpl.existsSync(statePath)) {
        const parsed = JSON.parse(String(fsImpl.readFileSync(statePath, 'utf8')))

        if (parsed && typeof parsed === 'object' && typeof parsed.code_sha === 'string' && parsed.code_sha.length >= 7) {
          return parsed.code_sha
        }
      }
    } catch {
      // ignore
    }
  }

  return null
}

/** The dev run's revision: git HEAD of the Hermes root, else the backend code_sha, else null. */
export function loadDevRevisionStamp(options: LoadDevRevisionStampOptions = {}): Readonly<DevRevisionStamp> | null {
  const fsImpl = options.fsImpl || fs
  const root = options.hermesRoot || process.env.HERMES_DESKTOP_HERMES_ROOT || options.appRoot || process.cwd()
  const runGit = options.runGitSync || defaultRunGitSync
  const gitHead = runGit(['rev-parse', 'HEAD'], root)

  if (gitHead && typeof gitHead === 'string' && gitHead.length >= 7) {
    const branch = runGit(['rev-parse', '--abbrev-ref', 'HEAD'], root)
    const dirtyStr = runGit(['status', '--porcelain'], root)

    return Object.freeze({
      commit: gitHead,
      branch: branch && branch !== 'HEAD' ? branch : null,
      dirty: Boolean(dirtyStr && dirtyStr.length > 0),
      source: 'git' as const
    })
  }

  const backendSha = resolveBackendCodeSha(options, fsImpl)

  if (backendSha) {
    return Object.freeze({ commit: backendSha, branch: null, dirty: false, source: 'backend' as const })
  }

  // Never fall back to a stale build stamp in dev mode.
  return null
}
