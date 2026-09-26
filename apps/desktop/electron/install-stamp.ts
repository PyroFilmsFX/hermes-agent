import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

export const INSTALL_STAMP_SCHEMA_VERSION = 1

export interface InstallStamp {
  schemaVersion: number
  commit: string
  branch?: string | null
  builtAt?: string | null
  dirty?: boolean
  source?: string | null
  path?: string | null
}

export interface FsReader {
  existsSync: (p: string) => boolean
  readFileSync: (p: string, encoding: any) => string
}

export interface LoadInstallStampOptions {
  /** Explicit dev mode flag. Defaults to checking HERMES_DESKTOP_DEV_SERVER. */
  isDev?: boolean
  /** Path to resources directory. Defaults to process.resourcesPath. */
  resourcesPath?: string | null
  /** App root directory. Defaults to process.cwd() or app root. */
  appRoot?: string
  /** Desktop repo root override (e.g. HERMES_DESKTOP_HERMES_ROOT). */
  hermesRoot?: string
  /** HERMES_HOME directory. */
  hermesHome?: string
  /** Fallback code sha from backend. */
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

function resolveBackendCodeSha(
  options: LoadInstallStampOptions,
  fsImpl: FsReader | typeof fs
): string | null {
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
        const raw = String(fsImpl.readFileSync(statePath, 'utf8'))
        const parsed = JSON.parse(raw)
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

export function loadInstallStamp(options: LoadInstallStampOptions = {}): InstallStamp | null {
  const fsImpl = options.fsImpl || fs
  const isDev =
    options.isDev !== undefined
      ? options.isDev
      : Boolean(process.env.HERMES_DESKTOP_DEV_SERVER)

  if (isDev) {
    // In DEV mode, derive the sha from git rev-parse HEAD of the desktop's repo root
    // (HERMES_DESKTOP_HERMES_ROOT or the app root), falling back to the backend-reported
    // code_sha, and never show a stale build stamp.
    const root =
      options.hermesRoot ||
      process.env.HERMES_DESKTOP_HERMES_ROOT ||
      options.appRoot ||
      (typeof process !== 'undefined' ? process.cwd() : '')

    const runGit = options.runGitSync || defaultRunGitSync

    const gitHead = runGit(['rev-parse', 'HEAD'], root)
    if (gitHead && typeof gitHead === 'string' && gitHead.length >= 7) {
      const branch = runGit(['rev-parse', '--abbrev-ref', 'HEAD'], root)
      const dirtyStr = runGit(['status', '--porcelain'], root)

      return Object.freeze({
        schemaVersion: INSTALL_STAMP_SCHEMA_VERSION,
        commit: gitHead,
        branch: branch && branch !== 'HEAD' ? branch : null,
        builtAt: null,
        dirty: Boolean(dirtyStr && dirtyStr.length > 0),
        source: 'git',
        path: null
      })
    }

    const backendSha = resolveBackendCodeSha(options, fsImpl)
    if (backendSha) {
      return Object.freeze({
        schemaVersion: INSTALL_STAMP_SCHEMA_VERSION,
        commit: backendSha,
        branch: null,
        builtAt: null,
        dirty: false,
        source: 'backend',
        path: null
      })
    }

    // Never return a stale build stamp in dev mode
    return null
  }

  const resourcesPath =
    options.resourcesPath !== undefined
      ? options.resourcesPath
      : typeof process !== 'undefined'
        ? process.resourcesPath
        : null
  const appRoot = options.appRoot || (typeof process !== 'undefined' ? process.cwd() : '')

  const candidates = [
    resourcesPath ? path.join(resourcesPath, 'install-stamp.json') : null,
    appRoot ? path.join(appRoot, 'build', 'install-stamp.json') : null
  ].filter(Boolean) as string[]

  for (const p of candidates) {
    try {
      const raw = String(fsImpl.readFileSync(p, 'utf8'))
      const parsed = JSON.parse(raw)

      if (parsed && typeof parsed === 'object' && typeof parsed.commit === 'string' && parsed.commit.length >= 7) {
        if (parsed.schemaVersion !== INSTALL_STAMP_SCHEMA_VERSION) {
          console.warn(
            `[hermes] install-stamp.json schemaVersion ${parsed.schemaVersion} != expected ${INSTALL_STAMP_SCHEMA_VERSION}; ignoring`
          )
          continue
        }

        return Object.freeze({
          schemaVersion: parsed.schemaVersion,
          commit: parsed.commit,
          branch: parsed.branch || null,
          builtAt: parsed.builtAt || null,
          dirty: Boolean(parsed.dirty),
          source: parsed.source || null,
          path: p
        })
      }
    } catch {
      // Either ENOENT or malformed JSON; try the next candidate
    }
  }

  return null
}
