import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  addWorktree,
  ensureGitRepo,
  listBaseBranches,
  listBranches,
  listWorktrees,
  parseWorktrees,
  sanitizeBranch,
  switchBranch
} from './git-worktree-ops'

test('sanitizeBranch: spaces → hyphens, forbidden chars dropped, edges trimmed', () => {
  assert.equal(sanitizeBranch('beach vibes'), 'beach-vibes')
  assert.equal(sanitizeBranch('feat/cool thing'), 'feat/cool-thing')
  assert.equal(sanitizeBranch('  wip~^:? '), 'wip')
  assert.equal(sanitizeBranch('///'), '')
})

test('parseWorktrees: main checkout + linked worktree', () => {
  const out = [
    'worktree /repo',
    'HEAD abc123',
    'branch refs/heads/main',
    '',
    'worktree /repo/.worktrees/feat',
    'HEAD def456',
    'branch refs/heads/hermes/feat',
    ''
  ].join('\n')

  const trees = parseWorktrees(out)

  assert.equal(trees.length, 2)
  assert.equal(trees[0].path, '/repo')
  assert.equal(trees[0].branch, 'main')
  assert.equal(trees[1].path, '/repo/.worktrees/feat')
  assert.equal(trees[1].branch, 'hermes/feat')
})

test('parseWorktrees: detached + locked flags', () => {
  const out = ['worktree /repo/wt', 'HEAD abc', 'detached', 'locked reason', ''].join('\n')
  const trees = parseWorktrees(out)

  assert.equal(trees.length, 1)
  assert.equal(trees[0].detached, true)
  assert.equal(trees[0].locked, true)
  assert.equal(trees[0].branch, null)
})

test('parseWorktrees: empty input', () => {
  assert.deepEqual(parseWorktrees(''), [])
})

test('listWorktrees marks branches merged into the default branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-worktrees-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    git('init', '-b', 'main')
    git('config', 'user.name', 'Hermes Test')
    git('config', 'user.email', 'hermes@example.test')
    fs.writeFileSync(path.join(dir, 'README'), 'root\n')
    git('add', 'README')
    git('commit', '-m', 'root')
    const root = git('rev-parse', 'HEAD')

    git('switch', '-c', 'feature/merged')
    fs.writeFileSync(path.join(dir, 'merged.txt'), 'merged\n')
    git('add', 'merged.txt')
    git('commit', '-m', 'merged change')
    git('switch', 'main')
    git('merge', '--ff-only', 'feature/merged')
    git('worktree', 'add', path.join(dir, 'merged-wt'), 'feature/merged')
    git('worktree', 'add', '-b', 'feature/unmerged', path.join(dir, 'unmerged-wt'), root)
    fs.writeFileSync(path.join(dir, 'unmerged-wt', 'pending.txt'), 'pending\n')
    execFileSync('git', ['add', 'pending.txt'], { cwd: path.join(dir, 'unmerged-wt') })
    execFileSync('git', ['commit', '-m', 'unmerged change'], { cwd: path.join(dir, 'unmerged-wt') })

    const byBranch = Object.fromEntries((await listWorktrees(dir, 'git')).map(tree => [tree.branch, tree]))

    assert.equal(byBranch['feature/merged'].merged, true)
    assert.equal(byBranch['feature/unmerged'].merged, false)
    assert.equal(byBranch.main.merged, false)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listWorktrees ignores an init.defaultBranch that does not exist and uses a present trunk', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-worktrees-default-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir, stdio: 'pipe' }).toString().trim()

  try {
    git('init', '-b', 'main')
    git('config', 'init.defaultBranch', 'missing-trunk')
    git('config', 'user.name', 'Hermes Test')
    git('config', 'user.email', 'hermes@example.test')
    fs.writeFileSync(path.join(dir, 'README'), 'root\n')
    git('add', 'README')
    git('commit', '-m', 'root')
    git('switch', '-c', 'feature/merged')
    fs.writeFileSync(path.join(dir, 'merged.txt'), 'merged\n')
    git('add', 'merged.txt')
    git('commit', '-m', 'merged')
    git('switch', 'main')
    git('merge', '--ff-only', 'feature/merged')
    git('worktree', 'add', path.join(dir, 'feature-wt'), 'feature/merged')

    const trees = await listWorktrees(dir, 'git')
    assert.equal(trees.find(tree => tree.branch === 'feature/merged')?.merged, true)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listWorktrees reports no merged branches when no default branch resolves', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-worktrees-no-default-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir, stdio: 'pipe' }).toString().trim()

  try {
    git('init', '-b', 'topic')
    git('config', 'init.defaultBranch', 'missing-trunk')
    git('config', 'user.name', 'Hermes Test')
    git('config', 'user.email', 'hermes@example.test')
    fs.writeFileSync(path.join(dir, 'README'), 'root\n')
    git('add', 'README')
    git('commit', '-m', 'root')
    git('branch', 'feature/branch')
    git('worktree', 'add', path.join(dir, 'feature-wt'), 'feature/branch')

    const trees = await listWorktrees(dir, 'git')
    assert.ok(trees.every(tree => tree.merged === false))
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

// A throwaway repo with an identity, and a helper that runs git in any of its
// directories (the main checkout by default).
function tempRepo(prefix: string, initArgs: string[] = ['init', '-b', 'main']) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  const git = (...args: string[]) => execFileSync('git', args, { cwd: dir, stdio: 'pipe' }).toString().trim()

  const gitIn = (cwd: string, ...args: string[]) => execFileSync('git', args, { cwd, stdio: 'pipe' }).toString().trim()

  git(...initArgs)
  git('config', 'user.name', 'Hermes Test')
  git('config', 'user.email', 'hermes@example.test')
  git('config', 'commit.gpgsign', 'false')

  const commitFile = (cwd: string, name: string, body = `${name}\n`) => {
    fs.writeFileSync(path.join(cwd, name), body)
    gitIn(cwd, 'add', name)
    gitIn(cwd, 'commit', '-m', `add ${name}`)

    return gitIn(cwd, 'rev-parse', 'HEAD')
  }

  return { commitFile, dir, git, gitIn }
}

test('listWorktrees resolves a trunk that exists only as origin/<trunk> (no local trunk branch)', async () => {
  const upstream = tempRepo('hermes-wt-upstream-')
  const cloneDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-wt-clone-'))
  const git = (...args: string[]) => execFileSync('git', args, { cwd: cloneDir, stdio: 'pipe' }).toString().trim()

  try {
    upstream.commitFile(upstream.dir, 'README')
    upstream.git('switch', '-c', 'feature/merged')
    upstream.commitFile(upstream.dir, 'merged.txt')
    upstream.git('switch', 'main')
    upstream.git('merge', '--ff-only', 'feature/merged')

    execFileSync('git', ['clone', '--quiet', upstream.dir, cloneDir], { stdio: 'pipe' })
    git('config', 'user.name', 'Hermes Test')
    git('config', 'user.email', 'hermes@example.test')
    git('config', 'commit.gpgsign', 'false')

    // The main checkout sits on a topic branch and the local trunk is gone:
    // trunk now exists ONLY as origin/main.
    git('switch', '-c', 'topic')
    git('branch', '-D', 'main')
    git('branch', 'feature/merged', 'origin/feature/merged')
    git('worktree', 'add', path.join(cloneDir, 'merged-wt'), 'feature/merged')
    git('worktree', 'add', '-b', 'feature/unmerged', path.join(cloneDir, 'unmerged-wt'), 'origin/main')
    fs.writeFileSync(path.join(cloneDir, 'unmerged-wt', 'pending.txt'), 'pending\n')
    execFileSync('git', ['add', 'pending.txt'], { cwd: path.join(cloneDir, 'unmerged-wt') })
    execFileSync('git', ['commit', '-m', 'pending'], { cwd: path.join(cloneDir, 'unmerged-wt') })

    const byBranch = Object.fromEntries((await listWorktrees(cloneDir, 'git')).map(tree => [tree.branch, tree]))

    assert.equal(byBranch['feature/merged'].merged, true)
    assert.equal(byBranch['feature/merged'].mergedVia, 'merged-ancestor')
    assert.equal(byBranch['feature/unmerged'].merged, false)
    assert.equal(byBranch['feature/unmerged'].mergedVia, null)
  } finally {
    fs.rmSync(upstream.dir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('listWorktrees treats a branch merged into origin/<trunk> as merged while the local trunk is stale', async () => {
  const upstream = tempRepo('hermes-wt-upstream-stale-')
  const cloneDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-wt-clone-stale-'))
  const git = (...args: string[]) => execFileSync('git', args, { cwd: cloneDir, stdio: 'pipe' }).toString().trim()

  try {
    upstream.commitFile(upstream.dir, 'README')
    execFileSync('git', ['clone', '--quiet', upstream.dir, cloneDir], { stdio: 'pipe' })
    git('config', 'user.name', 'Hermes Test')
    git('config', 'user.email', 'hermes@example.test')
    git('config', 'commit.gpgsign', 'false')

    // Work lands upstream (the PR merged on the remote); the clone fetches, but
    // its local main is never fast-forwarded.
    upstream.git('switch', '-c', 'feature/landed')
    upstream.commitFile(upstream.dir, 'landed.txt')
    upstream.git('switch', 'main')
    upstream.git('merge', '--ff-only', 'feature/landed')
    git('fetch', '--quiet', 'origin')
    git('branch', 'feature/landed', 'origin/feature/landed')
    git('worktree', 'add', path.join(cloneDir, 'landed-wt'), 'feature/landed')

    const landed = (await listWorktrees(cloneDir, 'git')).find(tree => tree.branch === 'feature/landed')

    assert.equal(landed?.merged, true)
    assert.equal(landed?.mergedVia, 'merged-ancestor')
  } finally {
    fs.rmSync(upstream.dir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('listWorktrees proves squash/rebase merges with git cherry and fails safe on partial ones', async () => {
  const { commitFile, dir, git, gitIn } = tempRepo('hermes-wt-cherry-')

  try {
    commitFile(dir, 'README')

    // One-commit branch, squash-merged into main.
    const squashWt = path.join(dir, 'squash-wt')
    git('worktree', 'add', '-b', 'feature/squash', squashWt, 'main')
    commitFile(squashWt, 'squash.txt')
    git('merge', '--squash', 'feature/squash')
    git('commit', '-m', 'Squash feature/squash (#1)')

    // Two-commit branch, rebase-merged (each commit replayed onto main).
    const rebaseWt = path.join(dir, 'rebase-wt')
    git('worktree', 'add', '-b', 'feature/rebased', rebaseWt, 'main~1')
    const r1 = commitFile(rebaseWt, 'r1.txt')
    const r2 = commitFile(rebaseWt, 'r2.txt')
    git('cherry-pick', r1, r2)

    // Two-commit branch with only ONE commit landed: not accounted for.
    const partialWt = path.join(dir, 'partial-wt')
    git('worktree', 'add', '-b', 'feature/partial', partialWt, 'main')
    const p1 = commitFile(partialWt, 'p1.txt')
    commitFile(partialWt, 'p2.txt')
    git('cherry-pick', p1)

    // Never landed.
    const openWt = path.join(dir, 'open-wt')
    git('worktree', 'add', '-b', 'feature/open', openWt, 'main')
    commitFile(openWt, 'open.txt')

    const byBranch = Object.fromEntries((await listWorktrees(dir, 'git')).map(tree => [tree.branch, tree]))

    assert.equal(byBranch['feature/squash'].mergedVia, 'merged-squash')
    assert.equal(byBranch['feature/squash'].merged, true)
    assert.equal(byBranch['feature/rebased'].mergedVia, 'merged-squash')
    assert.equal(byBranch['feature/partial'].mergedVia, null)
    assert.equal(byBranch['feature/partial'].merged, false)
    assert.equal(byBranch['feature/open'].mergedVia, null)
    assert.equal(byBranch['feature/open'].merged, false)
    // The trunk itself is never reported as merged into itself.
    assert.equal(byBranch.main.merged, false)
    // A second pass (served from the cherry cache) gives the same answer.
    const again = (await listWorktrees(dir, 'git')).find(tree => tree.branch === 'feature/squash')
    assert.equal(again?.mergedVia, 'merged-squash')
    assert.ok(gitIn(squashWt, 'status', '--porcelain') === '')
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listWorktrees reports whether each linked worktree is clean, and unknown when it cannot tell', async () => {
  const { commitFile, dir, git } = tempRepo('hermes-wt-clean-')

  try {
    commitFile(dir, 'README')
    const cleanWt = path.join(dir, 'clean-wt')
    const untrackedWt = path.join(dir, 'untracked-wt')
    const modifiedWt = path.join(dir, 'modified-wt')
    const goneWt = path.join(dir, 'gone-wt')

    git('worktree', 'add', '-b', 'lane/clean', cleanWt, 'main')
    git('worktree', 'add', '-b', 'lane/untracked', untrackedWt, 'main')
    git('worktree', 'add', '-b', 'lane/modified', modifiedWt, 'main')
    git('worktree', 'add', '-b', 'lane/gone', goneWt, 'main')
    fs.writeFileSync(path.join(untrackedWt, 'scratch.txt'), 'scratch\n')
    fs.writeFileSync(path.join(modifiedWt, 'README'), 'edited\n')
    // The directory vanished without `git worktree remove`: status cannot run.
    fs.rmSync(goneWt, { recursive: true, force: true })

    const byBranch = Object.fromEntries((await listWorktrees(dir, 'git')).map(tree => [tree.branch, tree]))

    assert.equal(byBranch['lane/clean'].clean, true)
    assert.equal(byBranch['lane/untracked'].clean, false)
    assert.equal(byBranch['lane/modified'].clean, false)
    assert.equal(byBranch['lane/gone'].clean, null)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('ensureGitRepo: inits a plain dir with a root commit so worktrees branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-wt-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    assert.match(git('rev-parse', '--verify', 'HEAD'), /^[0-9a-f]{7,}$/)

    // The whole point: a worktree can now branch off the seeded root commit.
    execFileSync('git', ['worktree', 'add', '-b', 'wt', path.join(dir, '.worktrees', 'wt')], { cwd: dir })
    assert.ok(fs.existsSync(path.join(dir, '.worktrees', 'wt')))

    // Idempotent: an already-committed repo gets no extra commit.
    await ensureGitRepo('git', dir)
    assert.equal(git('rev-list', '--count', 'HEAD'), '1')
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('switchBranch: switches a normal checkout branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-switch-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    execFileSync('git', ['branch', 'feature'], { cwd: dir })

    await switchBranch(dir, 'feature', 'git')

    assert.equal(git('branch', '--show-current'), 'feature')
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBranches: lists locals and flags the checked-out branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-branches-'))

  try {
    await ensureGitRepo('git', dir)
    const current = execFileSync('git', ['branch', '--show-current'], { cwd: dir }).toString().trim()
    execFileSync('git', ['branch', 'feature'], { cwd: dir })

    const branches = await listBranches(dir, 'git')
    const names = branches.map(b => b.name).sort()

    assert.deepEqual(names, [current, 'feature'].sort())
    // The repo's own checkout is flagged; the unused branch is convertible.
    assert.equal(branches.find(b => b.name === current).checkedOut, true)
    assert.equal(branches.find(b => b.name === current).isDefault, true)
    assert.equal(fs.realpathSync(branches.find(b => b.name === current).worktreePath), fs.realpathSync(dir))
    assert.equal(branches.find(b => b.name === 'feature').checkedOut, false)
    assert.equal(branches.find(b => b.name === 'feature').isDefault, false)
    assert.equal(branches.find(b => b.name === 'feature').worktreePath, null)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBranches: flags a free default branch as default, not checked out', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-branches-default-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    const trunk = git('branch', '--show-current')
    execFileSync('git', ['switch', '-c', 'rawr'], { cwd: dir })

    const branches = await listBranches(dir, 'git')
    const defaultBranch = branches.find(b => b.name === trunk)

    assert.equal(defaultBranch.checkedOut, false)
    assert.equal(defaultBranch.isDefault, true)
    assert.equal(defaultBranch.worktreePath, null)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBranches: a branch claimed by a worktree is flagged checked out', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-branches-wt-'))

  try {
    await ensureGitRepo('git', dir)
    execFileSync('git', ['branch', 'feature'], { cwd: dir })
    // addWorktree converts the existing "feature" branch into a worktree.
    const result = await addWorktree(dir, { existingBranch: 'feature' }, 'git')

    assert.equal(result.branch, 'feature')
    assert.ok(fs.existsSync(result.path))

    const branches = await listBranches(dir, 'git')

    assert.equal(branches.find(b => b.name === 'feature').checkedOut, true)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBranches: empty on a non-repo path', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-nonrepo-'))

  try {
    assert.deepEqual(await listBranches(dir, 'git'), [])
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('addWorktree: existingBranch checks the branch out without a new branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-convert-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    execFileSync('git', ['branch', 'cool/feature'], { cwd: dir })

    const before = git('branch', '--list').split('\n').length
    const result = await addWorktree(dir, { existingBranch: 'cool/feature' }, 'git')

    // No new branch was created — only the existing one is checked out.
    assert.equal(git('branch', '--list').split('\n').length, before)
    assert.equal(result.branch, 'cool/feature')
    // Dir is named off the branch slug, nested under the main repo's .worktrees.
    assert.match(result.path, /[/\\]\.worktrees[/\\]cool-feature/)
    assert.equal(
      execFileSync('git', ['branch', '--show-current'], { cwd: result.path }).toString().trim(),
      'cool/feature'
    )
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('addWorktree: existing default branch switches the main checkout, not .worktrees/main', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-convert-default-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    const trunk = git('branch', '--show-current')
    execFileSync('git', ['switch', '-c', 'rawr'], { cwd: dir })

    const result = await addWorktree(dir, { existingBranch: trunk }, 'git')

    assert.equal(result.branch, trunk)
    assert.equal(fs.realpathSync(result.path), fs.realpathSync(dir))
    assert.equal(git('branch', '--show-current'), trunk)
    assert.equal(fs.existsSync(path.join(dir, '.worktrees', trunk)), false)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBaseBranches: lists local branches and flags the default', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-base-branches-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    const trunk = git('branch', '--show-current')
    execFileSync('git', ['branch', 'feature'], { cwd: dir })

    const branches = await listBaseBranches(dir, 'git')
    const names = branches.map(b => b.name).sort()

    assert.deepEqual(names, [trunk, 'feature'].sort())
    // No remote → all local.
    assert.equal(
      branches.every(b => !b.isRemote),
      true
    )
    // The trunk is flagged as the default.
    assert.equal(branches.find(b => b.name === trunk).isDefault, true)
    assert.equal(branches.find(b => b.name === 'feature').isDefault, false)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listBaseBranches: empty on a non-repo path', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-base-nonrepo-'))

  try {
    assert.deepEqual(await listBaseBranches(dir, 'git'), [])
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('addWorktree: base param branches off a specified local branch', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-base-add-'))
  const git = (...args) => execFileSync('git', args, { cwd: dir }).toString().trim()

  try {
    await ensureGitRepo('git', dir)
    execFileSync('git', ['branch', 'staging'], { cwd: dir })

    const result = await addWorktree(
      dir,
      { base: 'staging', branch: 'new-from-staging', name: 'new-from-staging' },
      'git'
    )

    assert.equal(result.branch, 'new-from-staging')
    assert.equal(git('-C', result.path, 'merge-base', 'HEAD', 'staging').length > 0, true)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('addWorktree: base origin/main does not set up upstream tracking', async () => {
  // Two repos: a bare "remote" and a clone, so origin/main resolves as a
  // remote-tracking ref — the condition that triggers auto-tracking.
  const remoteDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-remote-'))
  const cloneDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-clone-'))
  const git = (...args) => execFileSync('git', args, { cwd: cloneDir }).toString().trim()

  try {
    // Seed the remote with a commit on main. Inline identity so it works
    // on CI runners with no global git config.
    execFileSync('git', ['init', '-b', 'main', remoteDir])
    execFileSync('git', [
      '-C',
      remoteDir,
      '-c',
      'user.email=hermes@localhost',
      '-c',
      'user.name=Hermes',
      'commit',
      '--allow-empty',
      '-m',
      'root'
    ])

    // Clone so origin/main exists as a remote-tracking ref.
    execFileSync('git', ['clone', remoteDir, cloneDir])

    const result = await addWorktree(
      cloneDir,
      { base: 'origin/main', branch: 'feature-branch', name: 'feature-branch' },
      'git'
    )

    assert.equal(result.branch, 'feature-branch')

    // The new branch must NOT have an upstream — like `git checkout origin/main
    // && git checkout -b feature-branch`, not `git worktree add -b … origin/main`.
    let hasUpstream = true

    try {
      execFileSync('git', ['-C', result.path, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'])
    } catch {
      hasUpstream = false
    }

    assert.equal(hasUpstream, false)
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

// A pair of repos: a bare "remote" with `main` and the extra branches in
// `branches`, plus a clone of it. Returns both paths. The caller must remove
// them.
function seedRemoteAndClone(label, branches) {
  const remoteDir = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-${label}-remote-`))
  const cloneDir = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-${label}-clone-`))

  const remoteGit = (...args) =>
    execFileSync('git', ['-C', remoteDir, ...args])
      .toString()
      .trim()

  execFileSync('git', ['init', '-b', 'main', remoteDir])
  remoteGit('-c', 'user.email=hermes@localhost', '-c', 'user.name=Hermes', 'commit', '--allow-empty', '-m', 'root')

  for (const branch of branches) {
    remoteGit('branch', branch)
  }

  execFileSync('git', ['clone', remoteDir, cloneDir])

  return { cloneDir, remoteDir }
}

test('listBranches: offers remote branches that have no local counterpart', async () => {
  const { cloneDir, remoteDir } = seedRemoteAndClone('branches-remote', ['teammate-work'])

  try {
    const branches = await listBranches(cloneDir, 'git')
    const byName = new Map(branches.map(b => [b.name, b]))

    // The teammate's branch is only on the remote. The list therefore offers it
    // by its remote-tracking name, with a flag that lets the UI say "track
    // remote".
    const remoteOnly = byName.get('origin/teammate-work')

    assert.ok(remoteOnly)
    assert.equal(remoteOnly.isRemote, true)
    assert.equal(remoteOnly.checkedOut, false)
    assert.equal(remoteOnly.isDefault, false)
    assert.equal(remoteOnly.worktreePath, null)

    // `main` is checked out locally, so it shows once as a local branch.
    // "origin/main" is a duplicate of a branch that is already in the list.
    assert.equal(byName.get('main').isRemote, false)
    assert.equal(byName.has('origin/main'), false)

    // "origin/HEAD" is an alias for the default branch of the remote. It is not
    // a branch.
    assert.equal(
      branches.some(b => b.name.endsWith('/HEAD')),
      false
    )
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('addWorktree: a remote branch becomes a local branch tracking it', async () => {
  const { cloneDir, remoteDir } = seedRemoteAndClone('convert-remote', ['teammate-work'])

  try {
    const result = await addWorktree(cloneDir, { existingBranch: 'origin/teammate-work' }, 'git')

    const inTree = (...args) =>
      execFileSync('git', ['-C', result.path, ...args])
        .toString()
        .trim()

    // The worktree is on a local branch that has the name of the remote one. It
    // is not on a detached HEAD, which is the result of a checkout of
    // "origin/teammate-work".
    assert.equal(result.branch, 'teammate-work')
    assert.equal(inTree('branch', '--show-current'), 'teammate-work')
    assert.match(result.path, /[/\\]\.worktrees[/\\]teammate-work/)

    // The branch tracks the remote branch, so push and pull work with no more
    // setup.
    assert.equal(inTree('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'), 'origin/teammate-work')
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('addWorktree: a remote default branch gets its own worktree, not a home switch', async () => {
  const { cloneDir, remoteDir } = seedRemoteAndClone('convert-remote-default', [])

  const git = (...args) =>
    execFileSync('git', ['-C', cloneDir, ...args])
      .toString()
      .trim()

  try {
    // Move the main checkout off `main`, which makes "origin/main" convertible.
    // The local `main` is then free, but the request names the remote-tracking
    // ref.
    git('switch', '-c', 'rawr')
    git('branch', '-D', 'main')

    const result = await addWorktree(cloneDir, { existingBranch: 'origin/main' }, 'git')

    // "switch home" applies to a local default branch. A remote ref always gets
    // a new worktree, so the main checkout stays where the user put it.
    assert.equal(result.branch, 'main')
    assert.notEqual(fs.realpathSync(result.path), fs.realpathSync(cloneDir))
    assert.equal(git('branch', '--show-current'), 'rawr')
  } finally {
    fs.rmSync(remoteDir, { recursive: true, force: true })
    fs.rmSync(cloneDir, { recursive: true, force: true })
  }
})

test('switchBranch: non-repo dir short-circuits instead of throwing', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-sw-'))

  try {
    // A plain folder pinned as a project (no .git): its lane label is the
    // folder basename, not a branch — switching must no-op, not error, so
    // callers like "+" new session can proceed with a plain session.
    const result = await switchBranch(dir, '国创大赛', 'git')

    assert.deepEqual(result, { branch: null })
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('switchBranch: repo dir still validates the branch name and switches', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-sw-'))

  try {
    execFileSync('git', ['init', '-b', 'main'], { cwd: dir })
    execFileSync('git', ['config', 'user.email', 't@example.com'], { cwd: dir })
    execFileSync('git', ['config', 'user.name', 'test'], { cwd: dir })
    execFileSync('git', ['commit', '--allow-empty', '-m', 'root'], { cwd: dir })

    // Existing behaviour preserved: an illegal branch name still errors.
    await assert.rejects(() => switchBranch(dir, '///', 'git'), /Branch name is required/)

    // And switching to a real branch still works.
    const result = await switchBranch(dir, 'main', 'git')
    assert.deepEqual(result, { branch: 'main' })
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

// A `git` stand-in that logs each invocation, so a test can count how many git
// processes a listing really spawned.
function countingGit(dir) {
  const log = path.join(dir, '.git-calls.log')
  const bin = path.join(dir, 'counting-git.sh')
  const realGit = execFileSync('which', ['git']).toString().trim()

  fs.writeFileSync(bin, `#!/bin/sh\necho "$*" >> "${log}"\nexec "${realGit}" "$@"\n`, { mode: 0o755 })

  return {
    bin,
    calls: (needle) => (fs.existsSync(log) ? fs.readFileSync(log, 'utf8').split('\n').filter(line => line.includes(needle)).length : 0)
  }
}

function repoWithLane(prefix) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  const repo = path.join(dir, 'repo')
  fs.mkdirSync(repo)
  const git = (...args) => execFileSync('git', args, { cwd: repo, stdio: 'pipe' }).toString().trim()

  git('init', '-b', 'main')
  git('config', 'user.name', 'Hermes Test')
  git('config', 'user.email', 'hermes@example.test')
  fs.writeFileSync(path.join(repo, 'README'), 'root\n')
  git('add', 'README')
  git('commit', '-m', 'root')
  git('worktree', 'add', '-b', 'feature/lane', path.join(dir, 'lane-wt'))
  git('branch', 'feature/next')

  return { dir, repo }
}

test('listWorktrees: overlapping calls for one repo share at most one follow-up scan', async () => {
  const { dir, repo } = repoWithLane('hermes-worktrees-singleflight-')
  const git = countingGit(dir)

  try {
    const results = await Promise.all(Array.from({ length: 10 }, () => listWorktrees(repo, git.bin)))

    // The first call scans; the nine that arrive while it runs share ONE
    // follow-up scan that starts after it, so none gets a stale listing.
    assert.equal(git.calls('worktree list'), 2)
    assert.equal(git.calls(' status '), 2)

    for (const result of results) {
      assert.deepEqual(result, results[0])
    }

    // A later call is a fresh scan, never a replay of an old one.
    await listWorktrees(repo, git.bin)
    assert.equal(git.calls('worktree list'), 3)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listWorktrees: a lane added through addWorktree shows at once', async () => {
  const { dir, repo } = repoWithLane('hermes-worktrees-forget-')

  try {
    const before = await listWorktrees(repo, 'git')
    assert.equal(before.some(tree => tree.branch === 'feature/next'), false)

    await addWorktree(repo, { existingBranch: 'feature/next' }, 'git')

    const after = await listWorktrees(repo, 'git')
    assert.equal(after.some(tree => tree.branch === 'feature/next'), true)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('listWorktrees: a lane added by raw git while a scan runs shows in the next answer', async () => {
  const { dir, repo } = repoWithLane('hermes-worktrees-raced-')

  try {
    const first = listWorktrees(repo, 'git')
    execFileSync('git', ['worktree', 'add', path.join(dir, 'next-wt'), 'feature/next'], { cwd: repo, stdio: 'pipe' })
    const second = await listWorktrees(repo, 'git')

    await first
    assert.equal(second.some(tree => tree.branch === 'feature/next'), true)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
