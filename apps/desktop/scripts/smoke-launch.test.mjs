import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

import { afterAll, test } from 'vitest'

import { judgeSmokeRun, MAIN_PROCESS_ERROR, runSmoke } from './smoke-launch.mjs'

const scratch = mkdtempSync(join(tmpdir(), 'smoke-launch-test-'))

afterAll(() => rmSync(scratch, { recursive: true, force: true }))

// A stand-in for the Electron binary: a node script. No Electron is ever launched here.
function fakeApp(name, body) {
  const file = join(scratch, `${name}.mjs`)

  writeFileSync(file, body)

  return { command: process.execPath, args: [file], cwd: scratch, env: process.env }
}

const judge = over => judgeSmokeRun({ lines: [], stderr: '', code: 0, signal: null, timedOut: false, ...over })

test('verdict: READY then exit 0 passes; everything else fails', () => {
  assert.equal(judge({ lines: ['x', 'HERMES_DESKTOP_SMOKE_READY'] }).ok, true)
  assert.match(judge({}).reason, /without printing HERMES_DESKTOP_SMOKE_READY/)
  assert.match(judge({ lines: ['HERMES_DESKTOP_SMOKE_READY'], code: 1 }).reason, /exited with code 1/)
  assert.match(judge({ lines: ['HERMES_DESKTOP_SMOKE_READY'], code: null, signal: 'SIGSEGV' }).reason, /signal SIGSEGV/)
  assert.match(
    judge({ lines: ['HERMES_DESKTOP_SMOKE_READY', 'HERMES_DESKTOP_SMOKE_FAIL unhandledRejection x'] }).reason,
    /^HERMES_DESKTOP_SMOKE_FAIL unhandledRejection x$/
  )
  assert.match(judge({ lines: ['HERMES_DESKTOP_SMOKE_READY'], stderr: `${MAIN_PROCESS_ERROR}\n` }).reason, /stderr/)
  assert.match(judge({ lines: ['HERMES_DESKTOP_SMOKE_READY'], timedOut: true }).reason, /timed out/)
})

test('runSmoke passes a clean READY + exit 0 run', async () => {
  const app = fakeApp('ready', "console.log('booting'); console.log('HERMES_DESKTOP_SMOKE_READY'); process.exit(0)\n")
  const run = await runSmoke({ ...app, timeoutMs: 20_000 })

  assert.deepEqual(judgeSmokeRun(run), { ok: true, reason: 'main window finished loading and the app exited 0' })
})

test('runSmoke fails a FAIL line with exit 1', async () => {
  const app = fakeApp(
    'fail',
    "console.log(\"HERMES_DESKTOP_SMOKE_FAIL TypeError: Cannot read properties of undefined (reading 'resolve')\"); process.exit(1)\n"
  )
  const verdict = judgeSmokeRun(await runSmoke({ ...app, timeoutMs: 20_000 }))

  assert.equal(verdict.ok, false)
  assert.match(verdict.reason, /^HERMES_DESKTOP_SMOKE_FAIL TypeError/)
})

test('Electron’s error dialog text fails fast and the whole process tree is killed', async () => {
  const pidFile = join(scratch, 'grandchild.pid')
  // Like Electron after its default dialog: the process stays up (with a child of its own).
  const app = fakeApp(
    'hang',
    [
      "import { spawn } from 'node:child_process'",
      "import { writeFileSync } from 'node:fs'",
      "const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' })",
      `writeFileSync(${JSON.stringify(pidFile)}, String(child.pid))`,
      `console.error(${JSON.stringify(MAIN_PROCESS_ERROR)})`,
      'setInterval(() => {}, 1000)'
    ].join('\n')
  )
  const started = Date.now()
  const run = await runSmoke({ ...app, timeoutMs: 60_000 })
  const verdict = judgeSmokeRun(run)

  assert.ok(Date.now() - started < 20_000, 'did not wait out the timeout')
  assert.equal(verdict.ok, false)
  assert.match(verdict.reason, /A JavaScript error occurred in the main process/)
  const grandchild = Number(readFileSync(pidFile, 'utf8'))

  assert.throws(() => process.kill(grandchild, 0), /ESRCH/, 'grandchild reaped with the tree')
}, 30_000)

test('a silent hang hits the timeout and is killed', async () => {
  const app = fakeApp('silent', 'setInterval(() => {}, 1000)\n')
  const verdict = judgeSmokeRun(await runSmoke({ ...app, timeoutMs: 1_000 }))

  assert.deepEqual(verdict, { ok: false, reason: 'timed out waiting for the app to report and exit' })
}, 30_000)

test.skipIf(process.platform !== 'darwin')('refuses to launch on macOS without the explicit override', () => {
  const env = { ...process.env }

  delete env.HERMES_DESKTOP_SMOKE_ALLOW_DARWIN
  const result = spawnSync(process.execPath, [resolve(import.meta.dirname, 'smoke-launch.mjs')], {
    encoding: 'utf8',
    env,
    timeout: 10_000
  })

  assert.equal(result.status, 2)
  assert.match(result.stderr, /refusing to launch Electron on macOS/)
})
