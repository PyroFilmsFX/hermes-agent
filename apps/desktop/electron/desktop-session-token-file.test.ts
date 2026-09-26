import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, test } from 'vitest'

import {
  applySessionTokenHandoff,
  createSessionTokenFiles,
  createSessionTokenFileSupportResolver,
  helpDeclaresSessionTokenFile,
  SESSION_TOKEN_FILE_FLAG,
  sessionTokenRoot,
  sourceDeclaresSessionTokenFile
} from './desktop-session-token-file'

let home: string

beforeEach(() => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-token-file-'))
})

afterEach(() => {
  fs.rmSync(home, { force: true, recursive: true })
})

function files(platform = process.platform) {
  return createSessionTokenFiles({
    fs,
    homedir: () => home,
    randomHex: bytes => crypto.randomBytes(bytes).toString('hex'),
    platform
  })
}

const TOKEN = 'unit-test-token-value-not-a-secret-000000'

test('writes the token to a private one-shot file under ~/.hermes/desktop-local/<32hex>/<16hex>.token', () => {
  const store = files()
  const handoff = store.write(TOKEN)

  const relative = path.relative(sessionTokenRoot(home), handoff.file).split(path.sep)
  assert.equal(relative.length, 2)
  assert.match(relative[0], /^[0-9a-f]{32}$/)
  assert.match(relative[1], /^[0-9a-f]{16}\.token$/)
  assert.equal(path.dirname(handoff.file), handoff.dir)
  assert.equal(fs.readFileSync(handoff.file, 'utf8'), TOKEN)

  if (process.platform !== 'win32') {
    assert.equal(fs.statSync(sessionTokenRoot(home)).mode & 0o777, 0o700)
    assert.equal(fs.statSync(handoff.dir).mode & 0o777, 0o700)
    assert.equal(fs.statSync(handoff.file).mode & 0o777, 0o600)
  }
})

test('tightens a pre-existing loose token root to 0700', () => {
  if (process.platform === 'win32') {
    return
  }

  fs.mkdirSync(sessionTokenRoot(home), { mode: 0o755, recursive: true })
  fs.chmodSync(sessionTokenRoot(home), 0o755)
  files().write(TOKEN)
  assert.equal(fs.statSync(sessionTokenRoot(home)).mode & 0o777, 0o700)
})

test('every spawn gets a fresh directory and remove() deletes it exactly once', () => {
  const store = files()
  const a = store.write(TOKEN)
  const b = store.write(TOKEN)
  assert.notEqual(a.dir, b.dir)
  assert.deepEqual(new Set(store.liveDirs()), new Set([a.dir, b.dir]))

  store.remove(a.dir)
  assert.equal(fs.existsSync(a.dir), false)
  assert.equal(fs.existsSync(b.dir), true)
  store.remove(a.dir) // idempotent
  store.remove(null)
  store.removeAll()
  assert.equal(fs.existsSync(b.dir), false)
  assert.deepEqual(store.liveDirs(), [])
})

test('a directory remove() never created is not touched', () => {
  const store = files()
  const foreign = path.join(home, 'not-ours')
  fs.mkdirSync(foreign)
  store.remove(foreign)
  assert.equal(fs.existsSync(foreign), true)
})

test('refuses an empty token and cleans up when the exclusive create fails', () => {
  const store = files()
  assert.throws(() => store.write(''), /empty session token/)

  const failing = createSessionTokenFiles({
    fs: {
      ...fs,
      openSync: () => {
        throw Object.assign(new Error('EEXIST'), { code: 'EEXIST' })
      }
    } as never,
    homedir: () => home,
    randomHex: bytes => crypto.randomBytes(bytes).toString('hex'),
    platform: process.platform
  })

  assert.throws(() => failing.write(TOKEN), /EEXIST/)
  assert.deepEqual(failing.liveDirs(), [])
  assert.deepEqual(fs.readdirSync(sessionTokenRoot(home)), [])
})

test('file handoff: the flag goes on argv and NO token is left in the env', () => {
  const plan = applySessionTokenHandoff(
    ['--profile', 'p', 'serve', '--port', '0'],
    { PATH: '/bin', HERMES_DASHBOARD_SESSION_TOKEN: 'stale-inherited' },
    { dir: '/x', file: '/x/y.token' },
    TOKEN
  )

  assert.deepEqual(plan.args, ['--profile', 'p', 'serve', '--port', '0', SESSION_TOKEN_FILE_FLAG, '/x/y.token'])
  assert.equal('HERMES_DASHBOARD_SESSION_TOKEN' in plan.env, false)
  assert.equal(Object.values(plan.env).includes(TOKEN), false)
})

test('legacy runtimes keep the env handoff and get no unknown flag', () => {
  const plan = applySessionTokenHandoff(['serve'], { PATH: '/bin' }, null, TOKEN)
  assert.deepEqual(plan.args, ['serve'])
  assert.equal(plan.env.HERMES_DASHBOARD_SESSION_TOKEN, TOKEN)
})

test('flag detection never confuses the SSH flag for the local one', () => {
  assert.equal(sourceDeclaresSessionTokenFile('parser.add_argument(\n  "--session-token-file", dest="x")'), true)
  assert.equal(sourceDeclaresSessionTokenFile('"--ssh-session-token-file", dest="ssh_session_token_file"'), false)
  assert.equal(sourceDeclaresSessionTokenFile(null), false)
  assert.equal(helpDeclaresSessionTokenFile('  --session-token-file PATH\n'), true)
  assert.equal(helpDeclaresSessionTokenFile('  --ssh-session-token-file PATH\n'), false)
})

test('support resolver: source first, `serve --help` only when the source is unreadable, false when unsure', async () => {
  const helpCalls: string[][] = []
  let helpText = '  --session-token-file PATH'

  const resolve = createSessionTokenFileSupportResolver({
    readFile: async target => {
      if (target.startsWith('/new')) {
        return '"--session-token-file"'
      }

      if (target.startsWith('/old')) {
        return '"--ssh-session-token-file"'
      }

      throw new Error('ENOENT')
    },
    runHelp: async (_command, args) => {
      helpCalls.push(args)

      if (!helpText) {
        throw new Error('timeout')
      }

      return helpText
    },
    log: () => {}
  })

  assert.equal(await resolve({ command: 'py', root: '/new' }), true)
  assert.equal(await resolve({ command: 'py', root: '/old' }), false)
  assert.equal(helpCalls.length, 0)

  assert.equal(await resolve({ command: 'hermes' }), true)
  assert.deepEqual(helpCalls, [['serve', '--help']])
  assert.equal(await resolve({ command: 'hermes' }), true) // cached
  assert.equal(helpCalls.length, 1)

  helpText = ''
  assert.equal(await resolve({ command: 'py', args: ['-m', 'hermes_cli.main', 'serve'] }), false)
  assert.deepEqual(helpCalls[1], ['-m', 'hermes_cli.main', 'serve', '--help'])

  // Shell shims re-split argv: always the env handoff.
  assert.equal(await resolve({ command: 'hermes.cmd', root: '/new', shell: true }), false)
  assert.equal(await resolve({ command: '' }), false)
})

test('the desktop renderer takes the session token from the IPC bridge, never from a served page', () => {
  const desktopRoot = path.resolve(import.meta.dirname, '..')

  const walk = (dir: string): string[] =>
    fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
      const full = path.join(dir, entry.name)

      if (entry.isDirectory()) {
        return entry.name === 'node_modules' ? [] : walk(full)
      }

      return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [full] : []
    })

  const offenders = walk(path.join(desktopRoot, 'src')).filter(file =>
    fs.readFileSync(file, 'utf8').includes('__HERMES_SESSION_TOKEN__')
  )

  assert.deepEqual(offenders, [])

  const preload = fs.readFileSync(path.join(desktopRoot, 'electron', 'preload.ts'), 'utf8')
  assert.match(preload, /getConnection:\s*\(profile, opts\)\s*=>\s*ipcRenderer\.invoke\('hermes:connection'/)

  // Main hands the renderer the token it minted for a file-handoff spawn, not a `/` scrape.
  const main = fs.readFileSync(path.join(desktopRoot, 'electron', 'main.ts'), 'utf8')
  assert.equal((main.match(/const authToken = tokenHandoff\s*\?\s*token\s*:/g) || []).length, 2)
  assert.equal(main.includes('HERMES_DASHBOARD_SESSION_TOKEN: token'), false)
})
