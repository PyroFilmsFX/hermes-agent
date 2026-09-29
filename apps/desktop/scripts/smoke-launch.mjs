#!/usr/bin/env node
// CI launch smoke: start the BUILT app (`electron .` from apps/desktop, i.e. `npm run start`
// minus the build) headless with HERMES_DESKTOP_SMOKE=1 and require the main process to report
// HERMES_DESKTOP_SMOKE_READY and exit 0 (see electron/smoke-launch-guard.ts).
//
// Fails on: a HERMES_DESKTOP_SMOKE_FAIL line, a non-zero exit, Electron's default main-process
// error dialog text on stderr, or no exit within the timeout (the process tree is killed).
// The backend is not awaited: the renderer loads whether or not Python can start.
//
// Linux CI only (run it under xvfb-run). It refuses on macOS, where it would open a window on
// the owner's desktop, unless HERMES_DESKTOP_SMOKE_ALLOW_DARWIN=1.
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

import { isMain } from './utils.mjs'

export const READY_LINE = 'HERMES_DESKTOP_SMOKE_READY'
export const FAIL_PREFIX = 'HERMES_DESKTOP_SMOKE_FAIL'
export const MAIN_PROCESS_ERROR = 'A JavaScript error occurred in the main process'
export const DEFAULT_TIMEOUT_MS = 120_000
export const TAIL_LINES = 80

const appDir = resolve(import.meta.dirname, '..')

/**
 * Pure verdict over a finished (or killed) run. `lines` are all output lines in arrival order,
 * `stderr` is the raw stderr text.
 */
export function judgeSmokeRun({ lines, stderr, code, signal, timedOut }) {
  if (timedOut) {
    return { ok: false, reason: 'timed out waiting for the app to report and exit' }
  }

  const fail = lines.find(line => line.startsWith(FAIL_PREFIX))

  if (fail) {
    return { ok: false, reason: fail }
  }

  if (stderr.includes(MAIN_PROCESS_ERROR)) {
    return { ok: false, reason: `stderr: ${MAIN_PROCESS_ERROR}` }
  }

  if (code !== 0) {
    return { ok: false, reason: `app exited with ${code === null ? `signal ${signal}` : `code ${code}`}` }
  }

  if (!lines.some(line => line.trim() === READY_LINE)) {
    return { ok: false, reason: `app exited 0 without printing ${READY_LINE}` }
  }

  return { ok: true, reason: 'main window finished loading and the app exited 0' }
}

function killTree(child, signal) {
  if (!child.pid) {
    return
  }

  try {
    // detached: the child leads its own process group; a negative pid signals the whole group.
    process.kill(process.platform === 'win32' ? child.pid : -child.pid, signal)
  } catch {
    // already gone
  }
}

/** Spawn `command args` and collect output until it exits or the timeout kills its tree. */
export function runSmoke({ command, args, cwd, env, timeoutMs = DEFAULT_TIMEOUT_MS, echo = () => {} }) {
  return new Promise(resolvePromise => {
    const lines = []
    let stderr = ''
    let timedOut = false
    const child = spawn(command, args, { cwd, env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] })

    let doomed = false
    // Electron keeps running after its default main-process error dialog, and a FAIL line is
    // followed by app.exit(1): either way stop the tree shortly instead of waiting out the timeout.
    const doom = () => {
      if (doomed) {
        return
      }

      doomed = true
      setTimeout(() => killTree(child, 'SIGTERM'), 3_000).unref()
      setTimeout(() => killTree(child, 'SIGKILL'), 8_000).unref()
    }

    const collect = (stream, isErr) => {
      let pending = ''

      stream.setEncoding('utf8')
      stream.on('data', chunk => {
        if (isErr) {
          stderr += chunk
        }

        pending += chunk
        const parts = pending.split(/\r?\n/)

        pending = parts.pop() ?? ''

        for (const line of parts) {
          lines.push(line)
          echo(line)

          if (line.startsWith(FAIL_PREFIX)) {
            doom()
          }
        }

        if (isErr && stderr.includes(MAIN_PROCESS_ERROR)) {
          doom()
        }
      })

      return () => {
        if (pending) {
          lines.push(pending)
          echo(pending)
          pending = ''
        }
      }
    }

    const flushOut = collect(child.stdout, false)
    const flushErr = collect(child.stderr, true)

    const timer = setTimeout(() => {
      timedOut = true
      killTree(child, 'SIGTERM')
      setTimeout(() => killTree(child, 'SIGKILL'), 5_000).unref()
    }, timeoutMs)

    child.once('error', error => {
      clearTimeout(timer)
      lines.push(`${FAIL_PREFIX} could not spawn ${command}: ${error.message}`)
      resolvePromise({ lines, stderr, code: null, signal: null, timedOut })
    })

    child.once('exit', (code, signal) => {
      clearTimeout(timer)
      // Give the pipes a moment to drain, then reap anything the app left in its group
      // (GPU/utility helpers, a backend child) so CI never hangs on an orphan.
      setTimeout(() => {
        flushOut()
        flushErr()
        killTree(child, 'SIGKILL')
        resolvePromise({ lines, stderr, code, signal, timedOut })
      }, 250)
    })
  })
}

function electronBinary() {
  // The electron package's main export is the path of the downloaded binary (what `electron .` runs).
  return createRequire(join(appDir, 'package.json'))('electron')
}

async function main() {
  if (process.platform === 'darwin' && process.env.HERMES_DESKTOP_SMOKE_ALLOW_DARWIN !== '1') {
    console.error(
      'smoke-launch: refusing to launch Electron on macOS (it would open on the desktop). ' +
        'This smoke runs on the Linux CI runner; set HERMES_DESKTOP_SMOKE_ALLOW_DARWIN=1 to override.'
    )
    process.exit(2)
  }

  const hermesHome = mkdtempSync(join(tmpdir(), 'hermes-smoke-home-'))
  const env = {
    ...process.env,
    HERMES_DESKTOP_SMOKE: '1',
    HERMES_HOME: hermesHome,
    HERMES_DESKTOP_CDP_PORT: 'off'
  }

  delete env.ELECTRON_RUN_AS_NODE
  const args = ['.', ...(process.platform === 'linux' ? ['--no-sandbox'] : [])]
  const command = electronBinary()

  console.log(`smoke-launch: ${command} ${args.join(' ')} (cwd ${appDir}, HERMES_HOME ${hermesHome})`)
  const started = Date.now()
  const run = await runSmoke({ command, args, cwd: appDir, env, echo: line => console.log(`  | ${line}`) })
  const verdict = judgeSmokeRun(run)
  const seconds = ((Date.now() - started) / 1000).toFixed(1)

  rmSync(hermesHome, { recursive: true, force: true })

  if (verdict.ok) {
    console.log(`smoke-launch: PASS in ${seconds}s: ${verdict.reason}`)

    return
  }

  console.error(`smoke-launch: FAIL after ${seconds}s: ${verdict.reason}`)
  console.error(`--- last ${TAIL_LINES} lines of app output ---`)

  for (const line of run.lines.slice(-TAIL_LINES)) {
    console.error(line)
  }

  process.exit(1)
}

if (isMain(import.meta.url)) {
  await main()
}
