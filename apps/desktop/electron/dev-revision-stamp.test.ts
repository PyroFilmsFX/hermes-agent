import { describe, expect, it } from 'vitest'

import { loadDevRevisionStamp } from './dev-revision-stamp'

const STALE_BUILD_STAMP = {
  schemaVersion: 1,
  commit: 'c0deb072be4c0000000000000000000000000000',
  branch: 'main',
  builtAt: '2026-08-01T00:00:00.000Z',
  dirty: false,
  source: 'local'
}

const FRESH_GIT_HEAD_SHA = 'c0deb07c7a000000000000000000000000000000'
const BACKEND_CODE_SHA = 'c0deb07backend0000000000000000000000000'

function fakeFs(files: Record<string, string>) {
  return {
    existsSync: (p: string) => Object.prototype.hasOwnProperty.call(files, p),
    readFileSync: (p: string) => {
      if (Object.prototype.hasOwnProperty.call(files, p)) {
        return files[p]
      }
      throw new Error(`ENOENT: no such file or directory, open '${p}'`)
    }
  }
}

describe('loadDevRevisionStamp (dev runs)', () => {
  it('derives the sha from git rev-parse HEAD instead of the stale build stamp', () => {
    const fsImpl = fakeFs({
      '/app/build/install-stamp.json': JSON.stringify(STALE_BUILD_STAMP)
    })

    const runGitSync = (args: string[], cwd: string) => {
      if (args[0] === 'rev-parse' && args[1] === 'HEAD') {
        return FRESH_GIT_HEAD_SHA
      }
      if (args[0] === 'rev-parse' && args[1] === '--abbrev-ref') {
        return 'feature/dev-lane'
      }
      return ''
    }

    const stamp = loadDevRevisionStamp({
      appRoot: '/app',
      fsImpl,
      runGitSync
    })

    expect(stamp).not.toBeNull()
    expect(stamp?.commit).toBe(FRESH_GIT_HEAD_SHA)
    expect(stamp?.commit).not.toBe(STALE_BUILD_STAMP.commit)
    expect(stamp?.branch).toBe('feature/dev-lane')
    expect(stamp?.source).toBe('git')
  })

  it('honors HERMES_DESKTOP_HERMES_ROOT / hermesRoot when deriving git sha in dev mode', () => {
    const calls: { args: string[]; cwd: string }[] = []
    const runGitSync = (args: string[], cwd: string) => {
      calls.push({ args, cwd })
      return FRESH_GIT_HEAD_SHA
    }

    const stamp = loadDevRevisionStamp({
      appRoot: '/app',
      hermesRoot: '/custom/repo/root',
      runGitSync
    })

    expect(stamp?.commit).toBe(FRESH_GIT_HEAD_SHA)
    expect(calls[0].cwd).toBe('/custom/repo/root')
  })

  it('falls back to the backend-reported code_sha when git fails in dev mode', () => {
    const fsImpl = fakeFs({
      '/app/build/install-stamp.json': JSON.stringify(STALE_BUILD_STAMP),
      '/hermes-home/gateway_state.json': JSON.stringify({ code_sha: BACKEND_CODE_SHA })
    })

    // git fails
    const runGitSync = () => null

    const stamp = loadDevRevisionStamp({
      appRoot: '/app',
      hermesHome: '/hermes-home',
      fsImpl,
      runGitSync
    })

    expect(stamp).not.toBeNull()
    expect(stamp?.commit).toBe(BACKEND_CODE_SHA)
    expect(stamp?.commit).not.toBe(STALE_BUILD_STAMP.commit)
    expect(stamp?.source).toBe('backend')
  })

  it('falls back to explicit backendCodeSha option when provided and git fails', () => {
    const fsImpl = fakeFs({
      '/app/build/install-stamp.json': JSON.stringify(STALE_BUILD_STAMP)
    })

    const runGitSync = () => null

    const stamp = loadDevRevisionStamp({
      appRoot: '/app',
      backendCodeSha: BACKEND_CODE_SHA,
      fsImpl,
      runGitSync
    })

    expect(stamp?.commit).toBe(BACKEND_CODE_SHA)
    expect(stamp?.commit).not.toBe(STALE_BUILD_STAMP.commit)
  })

  it('never returns the stale build stamp in dev mode even if git and backend fail', () => {
    const fsImpl = fakeFs({
      '/app/build/install-stamp.json': JSON.stringify(STALE_BUILD_STAMP)
    })

    const runGitSync = () => null

    const stamp = loadDevRevisionStamp({
      appRoot: '/app',
      fsImpl,
      runGitSync
    })

    expect(stamp).toBeNull()
  })
})
