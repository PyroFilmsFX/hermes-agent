/**
 * CI launch smoke (`HERMES_DESKTOP_SMOKE=1`, driven by scripts/smoke-launch.mjs).
 *
 * Type-checks and unit tests cannot see a launch crash (a TDZ read at module
 * load crashed every launch in c0deb0749c). In smoke mode the main process
 * reports one line and exits:
 *   HERMES_DESKTOP_SMOKE_READY           the main window finished loading -> exit 0
 *   HERMES_DESKTOP_SMOKE_FAIL <message>  main-process throw/rejection, or the
 *                                        main window failed to load / lost its renderer -> exit 1
 *
 * main.ts imports this module FIRST (a side-effect import at the top of the
 * file), so the process handlers are installed before any other bundled module
 * body runs. Electron's ESM entry loader turns an evaluation error into
 * `process.emit('uncaughtException', err)`, which lands here. Outside smoke mode
 * nothing is installed and watchSmokeMainWindow is a no-op.
 */
import fs from 'node:fs'

import { app } from 'electron'

export const SMOKE_ENV = 'HERMES_DESKTOP_SMOKE'
export const SMOKE_READY = 'HERMES_DESKTOP_SMOKE_READY'
export const SMOKE_FAIL = 'HERMES_DESKTOP_SMOKE_FAIL'

interface SmokeProcess {
  on(event: 'uncaughtException' | 'unhandledRejection', listener: (reason: unknown) => void): unknown
}

interface SmokeWebContents {
  once(event: string, listener: (...args: any[]) => void): unknown
  on(event: string, listener: (...args: any[]) => void): unknown
}

export interface SmokeWindow {
  webContents: SmokeWebContents
  show?: () => void
  showInactive?: () => void
}

export interface SmokeGuardDeps {
  env: Record<string, string | undefined>
  proc: SmokeProcess
  exit: (code: number) => void
  write: (line: string) => void
}

export interface SmokeGuard {
  readonly active: boolean
  watchMainWindow(win: SmokeWindow): void
}

function describeReason(reason: unknown): string {
  const text = reason instanceof Error ? `${reason.name}: ${reason.message}` : String(reason)

  return text.replace(/\s+/g, ' ').trim() || 'unknown error'
}

export function createSmokeGuard(deps: SmokeGuardDeps): SmokeGuard {
  const active = deps.env[SMOKE_ENV] === '1'
  let settled = false

  const finish = (code: number, line: string) => {
    if (settled) {
      return
    }

    settled = true
    deps.write(line)
    deps.exit(code)
  }

  const fail = (reason: unknown) => finish(1, `${SMOKE_FAIL} ${describeReason(reason)}`)

  if (active) {
    deps.proc.on('uncaughtException', error => {
      if (!settled && error instanceof Error && error.stack) {
        deps.write(error.stack)
      }

      fail(error)
    })
    deps.proc.on('unhandledRejection', reason => fail(`unhandledRejection ${describeReason(reason)}`))
  }

  return {
    active,
    watchMainWindow(win) {
      if (!active) {
        return
      }

      // Created with show:false; the reveal path must not surface it either.
      win.show = () => {}
      win.showInactive = () => {}

      win.webContents.once('did-finish-load', () => finish(0, SMOKE_READY))
      win.webContents.on(
        'did-fail-load',
        (_event: unknown, errorCode: number, errorDescription: string, url: string, isMainFrame?: boolean) => {
          if (isMainFrame !== false) {
            fail(`did-fail-load ${errorCode} ${errorDescription} ${url}`)
          }
        }
      )
      win.webContents.on('render-process-gone', (_event: unknown, details: { reason?: string; exitCode?: number }) =>
        fail(`render-process-gone ${details?.reason ?? 'unknown'} (exit ${details?.exitCode ?? '?'})`)
      )
    }
  }
}

function writeLine(line: string) {
  try {
    fs.writeSync(1, `${line}\n`)
  } catch {
    console.log(line)
  }
}

function exitApp(code: number) {
  try {
    app.exit(code)
  } catch {
    process.exit(code)
  }
}

export const smokeGuard = createSmokeGuard({ env: process.env, proc: process, exit: exitApp, write: writeLine })

export function watchSmokeMainWindow(win: SmokeWindow): void {
  smokeGuard.watchMainWindow(win)
}
