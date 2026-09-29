import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

import ts from 'typescript'
import { afterAll, test, vi } from 'vitest'

import { createSmokeGuard, SMOKE_FAIL, SMOKE_READY } from './smoke-launch-guard'

vi.mock('electron', () => ({ app: { exit: vi.fn() } }))

const desktopDir = resolve(import.meta.dirname, '..')
const repoRoot = resolve(desktopDir, '../..')
const guardPath = join(desktopDir, 'electron/smoke-launch-guard.ts')
const scratch = mkdtempSync(join(tmpdir(), 'smoke-guard-test-'))

afterAll(() => rmSync(scratch, { recursive: true, force: true }))

function harness(env: Record<string, string | undefined>) {
  const proc = new EventEmitter()
  const out: string[] = []
  const exits: number[] = []
  const guard = createSmokeGuard({ env, proc, exit: code => exits.push(code), write: line => out.push(line) })

  return { guard, proc, out, exits }
}

function fakeWindow() {
  const webContents = new EventEmitter()
  const shown: string[] = []
  const win = {
    webContents,
    show: () => shown.push('show'),
    showInactive: () => shown.push('showInactive')
  }

  return { win, webContents, shown }
}

test('outside smoke mode nothing is installed and the window is left alone', () => {
  const { guard, proc, out, exits } = harness({})
  const { win, webContents, shown } = fakeWindow()

  assert.equal(guard.active, false)
  assert.equal(proc.listenerCount('uncaughtException'), 0)
  assert.equal(proc.listenerCount('unhandledRejection'), 0)
  guard.watchMainWindow(win)
  assert.equal(webContents.listenerCount('did-finish-load'), 0)
  win.show()
  assert.deepEqual(shown, ['show'])
  assert.deepEqual([out, exits], [[], []])
})

test('a main-process throw prints one FAIL line and exits 1, once', () => {
  const { proc, out, exits } = harness({ HERMES_DESKTOP_SMOKE: '1' })

  proc.emit('uncaughtException', new ReferenceError("Cannot access 'primaryProfilePin' before initialization"))
  proc.emit('uncaughtException', new Error('second'))
  assert.equal(out.at(-1), `${SMOKE_FAIL} ReferenceError: Cannot access 'primaryProfilePin' before initialization`)
  assert.equal(out.filter(line => line.startsWith(SMOKE_FAIL)).length, 1)
  assert.deepEqual(exits, [1])
})

test('an unhandled rejection fails the smoke', () => {
  const { proc, out, exits } = harness({ HERMES_DESKTOP_SMOKE: '1' })

  proc.emit('unhandledRejection', new Error('boom\nmore'))
  assert.deepEqual(out, [`${SMOKE_FAIL} unhandledRejection Error: boom more`])
  assert.deepEqual(exits, [1])
})

test('the main window finishing its load prints READY and exits 0; the window is never shown', () => {
  const { guard, out, exits } = harness({ HERMES_DESKTOP_SMOKE: '1' })
  const { win, webContents, shown } = fakeWindow()

  guard.watchMainWindow(win)
  win.show()
  win.showInactive()
  webContents.emit('did-finish-load')
  assert.deepEqual(shown, [])
  assert.deepEqual(out, [SMOKE_READY])
  assert.deepEqual(exits, [0])
})

test('a main-frame load failure or a gone renderer fails; a subframe failure does not', () => {
  const sub = harness({ HERMES_DESKTOP_SMOKE: '1' })
  const subWin = fakeWindow()

  sub.guard.watchMainWindow(subWin.win)
  subWin.webContents.emit('did-fail-load', {}, -6, 'ERR_FILE_NOT_FOUND', 'file:///x/frame.html', false)
  assert.deepEqual(sub.exits, [])

  const main = harness({ HERMES_DESKTOP_SMOKE: '1' })
  const mainWin = fakeWindow()

  main.guard.watchMainWindow(mainWin.win)
  mainWin.webContents.emit('did-fail-load', {}, -6, 'ERR_FILE_NOT_FOUND', 'file:///x/index.html', true)
  assert.deepEqual(main.out, [`${SMOKE_FAIL} did-fail-load -6 ERR_FILE_NOT_FOUND file:///x/index.html`])
  assert.deepEqual(main.exits, [1])

  const gone = harness({ HERMES_DESKTOP_SMOKE: '1' })
  const goneWin = fakeWindow()

  gone.guard.watchMainWindow(goneWin.win)
  goneWin.webContents.emit('render-process-gone', {}, { reason: 'crashed', exitCode: 139 })
  assert.deepEqual(gone.out, [`${SMOKE_FAIL} render-process-gone crashed (exit 139)`])
  assert.deepEqual(gone.exits, [1])
})

test('main.ts imports the smoke guard before anything else', () => {
  const source = readFileSync(join(desktopDir, 'electron/main.ts'), 'utf8')
  const file = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, false, ts.ScriptKind.TS)
  const firstImport = file.statements.find(ts.isImportDeclaration)

  assert.ok(firstImport)
  assert.equal((firstImport.moduleSpecifier as ts.StringLiteral).text, './smoke-launch-guard')
  assert.equal(firstImport.importClause, undefined, 'a side-effect import, so sorting keeps it first')
})

async function esbuild() {
  return import('esbuild')
}

// Electron 40's lib/browser/init.ts, reduced to the two pieces that matter: the default
// uncaughtException handler (a dialog, the process stays up) and the ESM entry loader, which
// routes an evaluation error to process.emit('uncaughtException').
const ELECTRON_LOADER = `
process.on('uncaughtException', function (error) {
  if (process.listenerCount('uncaughtException') > 1) return
  console.error('A JavaScript error occurred in the main process')
  console.error('Uncaught Exception:\\n' + error.stack)
  process.exitCode = 7
})
try {
  await import(process.argv[2])
} catch (err) {
  process.emit('uncaughtException', err)
}
`

test(
  'a TDZ read at module load (the c0deb0749c crash) is caught by the guard in the bundled main',
  async () => {
    const dir = mkdtempSync(join(scratch, 'tdz-'))
    const stub = join(dir, 'electron-stub.mjs')

    writeFileSync(stub, 'export const app = { exit(code) { process.exit(code) } }\n')
    // Same shape as the bug: a module-load expression reads a const declared further down.
    writeFileSync(
      join(dir, 'main.ts'),
      [
        `import ${JSON.stringify(guardPath)}`,
        'const early = currentPin()',
        'function currentPin() { return primaryProfilePin.value }',
        'const primaryProfilePin = { value: early }',
        'console.log("unreachable", primaryProfilePin)'
      ].join('\n')
    )
    writeFileSync(join(dir, 'loader.mjs'), ELECTRON_LOADER)
    const { build } = await esbuild()

    await build({
      entryPoints: [join(dir, 'main.ts')],
      bundle: true,
      platform: 'node',
      format: 'esm',
      target: 'node20',
      outfile: join(dir, 'electron-main.mjs'),
      alias: { electron: stub },
      logLevel: 'silent'
    })

    const run = (smoke: boolean) =>
      spawnSync(process.execPath, [join(dir, 'loader.mjs'), join(dir, 'electron-main.mjs')], {
        encoding: 'utf8',
        env: { ...process.env, HERMES_DESKTOP_SMOKE: smoke ? '1' : '' }
      })

    const smoke = run(true)

    assert.equal(smoke.status, 1, smoke.stdout + smoke.stderr)
    // esbuild hoists top-level const to var when bundling, so the bundled read is a TypeError on
    // undefined rather than a ReferenceError; both are module-load throws the guard must catch.
    assert.match(smoke.stdout, /^HERMES_DESKTOP_SMOKE_FAIL (TypeError|ReferenceError): .*\bvalue\b/m)
    assert.doesNotMatch(smoke.stdout + smoke.stderr, /unreachable|A JavaScript error occurred/)

    // Control: without smoke mode the guard is inert and Electron's default dialog path runs.
    const plain = run(false)

    assert.equal(plain.status, 7, plain.stdout + plain.stderr)
    assert.match(plain.stderr, /A JavaScript error occurred in the main process/)
    assert.doesNotMatch(plain.stdout, /HERMES_DESKTOP_SMOKE/)
  },
  60_000
)

// Statements that only define things. Anything else before the guard would run app code
// before the crash handlers exist.
function isInert(statement: ts.Statement, file: ts.SourceFile): boolean {
  if (ts.isImportDeclaration(statement) || ts.isFunctionDeclaration(statement)) {
    return true
  }

  if (ts.isExpressionStatement(statement)) {
    const call = statement.expression

    return ts.isCallExpression(call) && call.expression.getText(file) === '__export'
  }

  if (!ts.isVariableStatement(statement)) {
    return false
  }

  return statement.declarationList.declarations.every(({ initializer }) => {
    if (!initializer) {
      return true
    }

    if (ts.isArrowFunction(initializer) || ts.isFunctionExpression(initializer) || ts.isStringLiteral(initializer)) {
      return true
    }

    if (ts.isObjectLiteralExpression(initializer)) {
      return initializer.properties.length === 0
    }

    // esbuild runtime header: var __create = Object.create, __hasOwnProp = Object.prototype.hasOwnProperty, ...
    if (ts.isPropertyAccessExpression(initializer)) {
      return /^Object(\.prototype)?\.\w+$/.test(initializer.getText(file))
    }

    if (ts.isCallExpression(initializer)) {
      const callee = initializer.expression.getText(file)

      // Lazy module wrappers, the banner's createRequire, and esbuild's __require shim.
      return (
        ['__commonJS', '__esm', 'createRequire'].includes(callee) ||
        /^\(\(\w+\) => typeof require !== "undefined"/.test(callee)
      )
    }

    return false
  })
}

test(
  'in the real bundled main, only inert definitions precede the smoke guard installer',
  async () => {
    const out = mkdtempSync(join(scratch, 'bundle-'))
    const bundleModule = join(desktopDir, 'scripts/bundle-electron-main.mjs')
    const { bundleElectronMain } = await import(bundleModule)

    await bundleElectronMain({ source: repoRoot, out, dev: true })
    const text = readFileSync(join(out, 'electron-main.mjs'), 'utf8')
    const file = ts.createSourceFile('electron-main.mjs', text, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
    const installer = file.statements.findIndex(statement => statement.getText(file).includes('createSmokeGuard({'))

    assert.ok(installer > 0, 'the guard installer is in the bundle')
    const eager = file.statements
      .slice(0, installer)
      .filter(statement => !isInert(statement, file))
      .map(statement => statement.getText(file).slice(0, 120))

    assert.deepEqual(eager, [], 'statements that execute before the smoke guard installs its handlers')
    assert.ok(text.indexOf('// electron/smoke-launch-guard.ts') < text.indexOf('// electron/main.ts'))
  },
  120_000
)
