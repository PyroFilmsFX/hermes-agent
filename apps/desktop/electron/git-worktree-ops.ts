// Git-driven worktree operations for the desktop "Start work" flow: spin up a
// fresh worktree the lightest way (`git worktree add -b`), list real worktrees,
// and remove them. Git is the source of truth; the renderer just drives these.

import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import { resolveRequestedPathForIpc } from './hardening'

function runGit(gitBin, args, cwd): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      gitBin,
      args,
      { cwd, windowsHide: true, timeout: 30_000, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          err.stderr = String(stderr || '')
          reject(err)

          return
        }

        resolve(String(stdout || ''))
      }
    )
  })
}

// Parse `git worktree list --porcelain`. The first record is the main worktree.
function parseWorktrees(out) {
  const trees = []
  let cur = null

  for (const line of out.split('\n')) {
    if (line.startsWith('worktree ')) {
      if (cur) {
        trees.push(cur)
      }

      cur = { path: line.slice(9).trim(), branch: null, head: null, detached: false, bare: false, locked: false }
    } else if (!cur) {
      continue
    } else if (line.startsWith('HEAD ')) {
      cur.head = line.slice(5).trim() || null
    } else if (line.startsWith('branch ')) {
      cur.branch = line
        .slice(7)
        .trim()
        .replace(/^refs\/heads\//, '')
    } else if (line === 'detached') {
      cur.detached = true
    } else if (line === 'bare') {
      cur.bare = true
    } else if (line.startsWith('locked')) {
      cur.locked = true
    }
  }

  if (cur) {
    trees.push(cur)
  }

  return trees
}

// The trunk as refs that actually exist: the local branch, `origin/<trunk>`, or
// both. `defaultBranch` accepts a trunk that exists only on the remote, and a
// bare `git branch --merged main` resolves nothing there, so every lane read as
// unmerged. Refs that point at the same commit collapse into one.
async function resolveTrunkRefs(gitBin, cwd, trunk) {
  const refs = []
  const seen = new Set()

  for (const ref of [`refs/heads/${trunk}`, `refs/remotes/origin/${trunk}`]) {
    const sha = await gitLine(gitBin, ['rev-parse', '--verify', '--quiet', `${ref}^{commit}`], cwd)

    if (sha && !seen.has(sha)) {
      seen.add(sha)
      refs.push({ ref, sha })
    }
  }

  return refs
}

// Local branches reachable from any trunk ref (merged by ancestry). A failed
// probe adds nothing, so a branch is never reported merged on missing evidence.
async function ancestorMergedBranches(gitBin, cwd, trunkRefs) {
  const merged = new Set()

  for (const { ref } of trunkRefs) {
    const out = await gitLine(gitBin, ['for-each-ref', '--format=%(refname:short)', '--merged', ref, 'refs/heads'], cwd)

    for (const name of out.split('\n')) {
      if (name.trim()) {
        merged.add(name.trim())
      }
    }
  }

  return merged
}

// `git cherry` verdicts keyed by (repo, branch sha, trunk sha): exactly what the
// command consumes, so a hit is always still true. Only definite answers are
// stored; an errored probe is retried on the next pass.
const CHERRY_CACHE_MAX = 4096
const cherryCache = new Map()

// True when every commit the branch has over the trunk is patch-equivalent to
// one already in the trunk (a squash of a one-commit branch, or a rebase merge),
// and there is at least one such commit. False when any commit is unique. Null
// when git could not answer.
async function cherryMerged(gitBin, cwd, branch, branchSha, trunkSha) {
  const key = branchSha ? `${cwd}\0${branchSha}\0${trunkSha}` : ''

  if (key && cherryCache.has(key)) {
    return cherryCache.get(key)
  }

  let out

  try {
    out = await runGit(gitBin, ['cherry', trunkSha, `refs/heads/${branch}`], cwd)
  } catch {
    return null
  }

  const lines = out
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean)

  const verdict = lines.length > 0 && lines.every(line => line.startsWith('-'))

  if (key) {
    if (cherryCache.size >= CHERRY_CACHE_MAX) {
      cherryCache.delete(cherryCache.keys().next().value)
    }

    cherryCache.set(key, verdict)
  }

  return verdict
}

// Whether a worktree has no uncommitted changes (tracked or untracked). Null
// when status cannot run: the directory is gone, locked, or git failed.
// `--no-optional-locks` keeps this a pure read: no index refresh is written.
async function worktreeClean(gitBin, worktreePath) {
  try {
    const out = await runGit(gitBin, ['--no-optional-locks', 'status', '--porcelain'], worktreePath)

    return out.trim() === ''
  } catch {
    return null
  }
}

// Bounded parallel map: with ~200 worktrees an unbounded fan-out would spawn
// ~400 git processes at once.
const ACCOUNTING_CONCURRENCY = 8

async function mapBounded(items, limit, fn) {
  const results = new Array(items.length)
  let next = 0

  const worker = async () => {
    while (next < items.length) {
      const index = next++
      results[index] = await fn(items[index], index)
    }
  }

  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker))

  return results
}

// The cheap listing: `git worktree list` only. Callers that need a root or a
// branch-to-path map use this and skip the per-lane accounting below.
async function listWorktreeRecords(resolved, gitBin) {
  try {
    return parseWorktrees(await runGit(gitBin, ['worktree', 'list', '--porcelain'], resolved))
  } catch {
    return []
  }
}

// List worktrees for the sidebar, with the evidence the lane rollup needs to
// call a lane "done" (see src/app/chat/sidebar/projects/lane-accounting.ts):
//  - `mergedVia`: 'merged-ancestor' when the branch is an ancestor of the
//    local trunk or origin/<trunk>; 'merged-squash' when `git cherry` shows
//    every one of its commits already in the trunk; null when neither is
//    proven; absent for the main checkout, the trunk itself and detached trees.
//  - `clean`: `git status --porcelain` is empty; null when it could not run.
// `merged` stays the boolean the "merged" badge reads (either proof).
// Every probe fails toward "not merged" / "unknown", never toward done.
async function listWorktrees(repoPath, gitBin) {
  let resolved

  try {
    resolved = resolveRequestedPathForIpc(repoPath, { purpose: 'Worktree list' })
  } catch {
    return []
  }

  try {
    const out = await runGit(gitBin, ['worktree', 'list', '--porcelain'], resolved)
    const trees = parseWorktrees(out)
    const trunk = await defaultBranch(gitBin, resolved)
    const trunkRefs = trunk ? await resolveTrunkRefs(gitBin, resolved, trunk) : []
    const ancestorMerged = await ancestorMergedBranches(gitBin, resolved, trunkRefs)

    const evidence = await mapBounded(trees, ACCOUNTING_CONCURRENCY, async (tree, index) => {
      const lane = index > 0 && !tree.bare && tree.branch && !tree.detached && tree.branch !== trunk

      if (!lane) {
        return {}
      }

      let mergedVia = null

      if (ancestorMerged.has(tree.branch)) {
        mergedVia = 'merged-ancestor'
      } else {
        // Only lanes git did not already prove merged pay for `git cherry`.
        for (const { sha } of trunkRefs) {
          if (await cherryMerged(gitBin, resolved, tree.branch, tree.head, sha)) {
            mergedVia = 'merged-squash'

            break
          }
        }
      }

      return { mergedVia, clean: await worktreeClean(gitBin, tree.path) }
    })

    return trees.map((tree, index) => ({
      path: tree.path,
      branch: tree.branch,
      isMain: index === 0,
      detached: tree.detached,
      locked: tree.locked,
      merged: Boolean(evidence[index].mergedVia),
      ...evidence[index]
    }))
  } catch {
    return []
  }
}

// A git-ref-safe branch name (spaces → "-", drop forbidden chars, trim edges),
// or "" when nothing usable remains. Mirrors the renderer's `gitRef`, so a bad
// value can't reach `git` no matter the caller (the GUI also enforces live).
function sanitizeBranch(name) {
  return String(name || '')
    .replace(/\s+/g, '-')
    .replace(/[^\w./-]/g, '')
    .replace(/-{2,}/g, '-')
    .replace(/\/{2,}/g, '/')
    .replace(/\.{2,}/g, '.')
    .replace(/^[-./]+|[-./]+$/g, '')
}

function slugify(name) {
  const slug = String(name || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/g, '')

  return slug || 'work'
}

const TRUNK_BRANCHES = ['main', 'master', 'trunk', 'develop']

async function gitLine(gitBin, args, cwd) {
  try {
    return (await runGit(gitBin, args, cwd)).trim()
  } catch {
    return ''
  }
}

// True when the command exits 0. Use this function and not `gitLine` for a
// `--quiet` probe. A `--quiet` probe prints nothing when it finds the ref, and
// that output is the same as the output of a failure.
async function gitOk(gitBin, args, cwd) {
  try {
    await runGit(gitBin, args, cwd)

    return true
  } catch {
    return false
  }
}

// The remote that a ref belongs to ("origin" for "origin/main"), or "" when the
// name is not a remote-tracking ref in this repo. This function asks git. It
// does not assume that the remote has the name "origin", because a repo can
// give its remotes any name.
async function remoteOfRef(gitBin, cwd, name) {
  if (!name.includes('/')) {
    return ''
  }

  if (!(await gitOk(gitBin, ['show-ref', '--verify', '--quiet', `refs/remotes/${name}`], cwd))) {
    return ''
  }

  return name.slice(0, name.indexOf('/'))
}

async function defaultBranch(gitBin, cwd) {
  const remote = (
    await gitLine(gitBin, ['symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'], cwd)
  ).replace(/^origin\//, '')

  if (remote && (await branchExists(gitBin, cwd, remote))) {
    return remote
  }

  const configured = await gitLine(gitBin, ['config', '--get', 'init.defaultBranch'], cwd)

  if (configured && (await branchExists(gitBin, cwd, configured))) {
    return configured
  }

  for (const branch of TRUNK_BRANCHES) {
    if (await branchExists(gitBin, cwd, branch)) {
      return branch
    }
  }

  return ''
}

async function branchExists(gitBin, cwd, branch) {
  return (
    (await gitOk(gitBin, ['show-ref', '--verify', '--quiet', `refs/heads/${branch}`], cwd)) ||
    (await gitOk(gitBin, ['show-ref', '--verify', '--quiet', `refs/remotes/origin/${branch}`], cwd))
  )
}

// A brand-new project folder isn't a git repo — and a freshly-init'd one has no
// commit to branch from — so `git worktree add` would fail. Make the dir a repo
// with a root commit on the user's behalf so worktrees "just work". No-op for a
// repo that already has commits; never touches the user's files (the seed commit
// is `--allow-empty`), and never inits a dir that already lives inside a repo.
async function ensureGitRepo(gitBin, dir) {
  let needsRoot = false

  try {
    const inside = (await runGit(gitBin, ['rev-parse', '--is-inside-work-tree'], dir)).trim()

    if (inside !== 'true') {
      await runGit(gitBin, ['init'], dir)
      needsRoot = true
    } else {
      // Repo exists; a worktree still needs a HEAD to branch from.
      try {
        await runGit(gitBin, ['rev-parse', '--verify', 'HEAD'], dir)
      } catch {
        needsRoot = true
      }
    }
  } catch {
    await runGit(gitBin, ['init'], dir)
    needsRoot = true
  }

  if (needsRoot) {
    // Inline identity so the seed commit lands even with no global git config.
    await runGit(
      gitBin,
      [
        '-c',
        'user.email=hermes@localhost',
        '-c',
        'user.name=Hermes',
        'commit',
        '--allow-empty',
        '-m',
        'Initial commit'
      ],
      dir
    )
  }
}

// Resolve the repo's MAIN worktree root, so `.worktrees/` always nests under the
// primary checkout even when called from a linked worktree.
async function mainRoot(gitBin, cwd) {
  const [main] = await listWorktreeRecords(cwd, gitBin)

  return main ? main.path : cwd
}

function uniqueDir(base) {
  let dir = base
  let n = 1

  while (fs.existsSync(dir)) {
    n += 1
    dir = `${base}-${n}`
  }

  return dir
}

async function addExistingBranchWorktree(gitBin, root, name) {
  const requested = sanitizeBranch(name)

  if (!requested) {
    throw new Error('Branch name is required.')
  }

  // "origin/feature" is a remote-tracking ref and not a branch that git can
  // check out. `git worktree add <dir> origin/feature` detaches HEAD. Make a
  // local branch with the same short name that tracks the remote ref. This is
  // what `git switch feature` does for a branch on exactly one remote.
  const remote = await remoteOfRef(gitBin, root, requested)
  const branch = remote ? requested.slice(remote.length + 1) : requested

  if (!remote && branch === (await defaultBranch(gitBin, root))) {
    await runGit(gitBin, ['switch', branch], root)

    return { path: root, branch, repoRoot: root }
  }

  const dir = uniqueDir(path.join(root, '.worktrees', slugify(branch)))

  if (remote) {
    // The remote-tracking ref is stale if the user did not fetch recently. This
    // fetch is best effort: after a failure, the last known ref is still there
    // to branch from.
    try {
      await runGit(gitBin, ['fetch', remote, branch], root)
    } catch {
      // The user is offline, or the branch is gone from the remote. Use the ref
      // that the repo already has.
    }

    await runGit(gitBin, ['worktree', 'add', '--track', '-b', branch, dir, requested], root)

    return { path: dir, branch, repoRoot: root }
  }

  await runGit(gitBin, ['worktree', 'add', dir, branch], root)

  return { path: dir, branch, repoRoot: root }
}

async function addWorktree(repoPath, options, gitBin) {
  const resolved = resolveRequestedPathForIpc(repoPath, { purpose: 'Worktree add' })
  // A new project's folder may not be a git repo yet — init it (with a root
  // commit) so the worktree has something to branch from.
  await ensureGitRepo(gitBin, resolved)
  const root = await mainRoot(gitBin, resolved)
  const opts = options || {}

  if (opts.existingBranch) {
    return addExistingBranchWorktree(gitBin, root, opts.existingBranch)
  }

  const slug = slugify(opts.name || `work-${Date.now().toString(36)}`)
  const branch = sanitizeBranch(opts.branch) || `hermes/${slug}`
  const dir = uniqueDir(path.join(root, '.worktrees', slug))

  const args = ['worktree', 'add', '-b', branch, dir]

  if (opts.base) {
    // Remote-tracking branches may be stale or missing if the user hasn't
    // fetched recently. When the base is an `origin/…` ref, fetch just that
    // branch so `git worktree add -b new origin/main` works against the
    // latest remote commit. Local branches are used as-is.
    const base = String(opts.base)

    if (base.startsWith('origin/')) {
      const remoteBranch = base.slice('origin/'.length)

      try {
        await runGit(gitBin, ['fetch', 'origin', remoteBranch], root)
      } catch {
        // The fetch isn't mandatory, but it would be nice to do if possible.
        // If it's not possible, just use the local ref of the remote branch.
        // If it doesn't exist locally, we'll get an error
      }

      // When branching off a remote-tracking ref, git auto-sets up tracking
      // (e.g. `new-branch` → tracks `origin/main`). The user almost certainly
      // wants a standalone local branch — like `git checkout origin/main &&
      // git checkout -b new-branch` — not a branch silently wired to the
      // remote's upstream. `--no-track` prevents that.
      args.push('--no-track')
    }

    args.push(base)
  }

  try {
    await runGit(gitBin, args, root)
  } catch (err) {
    // Branch name may already exist — retry checking out the existing branch
    // into a fresh worktree dir instead of failing the whole flow.
    if (/already exists/i.test(err.stderr || '')) {
      await runGit(gitBin, ['worktree', 'add', dir, branch], root)
    } else {
      throw err
    }
  }

  return { path: dir, branch, repoRoot: root }
}

async function removeWorktree(repoPath, worktreePath, options, gitBin) {
  const resolvedRepo = resolveRequestedPathForIpc(repoPath, { purpose: 'Worktree remove (repo)' })
  const resolvedTree = resolveRequestedPathForIpc(worktreePath, { purpose: 'Worktree remove (tree)' })
  const root = await mainRoot(gitBin, resolvedRepo)
  const args = ['worktree', 'remove']

  if (options && options.force) {
    args.push('--force')
  }

  args.push(resolvedTree)
  await runGit(gitBin, args, root)

  return { removed: resolvedTree }
}

// List the branches for the "convert a branch into a worktree" picker, most
// recently committed first. The local heads come first. Then come the
// remote-tracking refs that have no local branch yet. This is the same set that
// the base-branch picker offers, so "convert" can reach a teammate's branch
// that the user did not check out.
// Each branch carries a flag for a checkout in a worktree, and the path of that
// worktree. Empty on a non-repo or a remote backend, where the probe cannot
// run.
async function listBranches(repoPath, gitBin) {
  let resolved

  try {
    resolved = resolveRequestedPathForIpc(repoPath, { purpose: 'Branch list' })
  } catch {
    return []
  }

  try {
    // Both children own cwd handles: a failed probe must still wait for its
    // sibling before the caller may remove or switch the repository directory.
    const probes = await Promise.allSettled([
      runGit(gitBin, ['for-each-ref', '--format=%(refname:short)', '--sort=-committerdate', 'refs/heads'], resolved),
      runGit(gitBin, ['for-each-ref', '--format=%(refname:short)', '--sort=-committerdate', 'refs/remotes'], resolved)
    ])

    const [local, remote] = probes

    if (local.status === 'rejected' || remote.status === 'rejected') {
      return []
    }

    const [localOut, remoteOut] = [local.value, remote.value]

    const trees = await listWorktreeRecords(resolved, gitBin)
    const pathByBranch = new Map(trees.filter(tree => tree.branch).map(tree => [tree.branch, tree.path]))
    const trunk = await defaultBranch(gitBin, resolved)

    const names = (out: string) =>
      out
        .split('\n')
        .map(line => line.trim())
        .filter(Boolean)

    const locals = names(localOut)
    const localSet = new Set(locals)

    const remotes = names(remoteOut).filter(name => {
      // "origin/HEAD" is a symbolic alias for the default branch of the remote.
      // It is not a branch, and it shows in the list as a duplicate.
      if (name.endsWith('/HEAD')) {
        return false
      }

      // The user reaches a remote branch that they track locally through its
      // local head. To list both is noise, and a checkout of the
      // remote-tracking ref detaches HEAD.
      return !localSet.has(name.slice(name.indexOf('/') + 1))
    })

    return [
      ...locals.map(name => ({
        name,
        checkedOut: pathByBranch.has(name),
        isDefault: Boolean(trunk && name === trunk),
        isRemote: false,
        worktreePath: pathByBranch.get(name) || null
      })),
      ...remotes.map(name => ({
        // A remote branch has no local checkout, and it cannot be the local
        // trunk. It is therefore never checked out and never the default.
        name,
        checkedOut: false,
        isDefault: false,
        isRemote: true,
        worktreePath: null
      }))
    ]
  } catch {
    return []
  }
}

async function switchBranch(repoPath, branch, gitBin) {
  const resolved = resolveRequestedPathForIpc(repoPath, { purpose: 'Branch switch' })

  // Sidebar lanes exist for plain folders too (non-repo explicit projects),
  // and their lane label is the folder basename — not a branch. `git switch`
  // there is meaningless, and sanitizing that label would throw a misleading
  // "Branch name is required." — so short-circuit for non-repo roots and let
  // callers (e.g. "+" new session on the project lane) proceed with a plain
  // session instead of aborting.
  let inside = 'false'

  try {
    inside = (await runGit(gitBin, ['rev-parse', '--is-inside-work-tree'], resolved)).trim()
  } catch {
    // Not a git repo (or git unavailable): fall through to the short-circuit.
  }

  if (inside !== 'true') {
    return { branch: null }
  }

  const target = sanitizeBranch(branch)

  if (!target) {
    throw new Error('Branch name is required.')
  }

  await runGit(gitBin, ['switch', target], resolved)

  return { branch: target }
}

// Branches the new worktree can be based on: local heads + remote-tracking
// refs. Listed most-recently-committed first; the remote's default branch
// (origin/HEAD) is flagged so the UI can preselect it. Empty on a non-repo /
// remote backend where the probe can't run.
async function listBaseBranches(repoPath, gitBin) {
  let resolved

  try {
    resolved = resolveRequestedPathForIpc(repoPath, { purpose: 'Base branch list' })
  } catch {
    return []
  }

  try {
    const out = await runGit(
      gitBin,
      [
        'for-each-ref',
        '--format=%(refname:short)\t%(committerdate:iso)',
        '--sort=-committerdate',
        'refs/heads',
        'refs/remotes'
      ],
      resolved
    )

    const remoteDefault = await gitLine(
      gitBin,
      ['symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'],
      resolved
    )

    const localDefault = await defaultBranch(gitBin, resolved)

    return out
      .split('\n')
      .map(line => line.trim())
      .filter(Boolean)
      .map(line => {
        const [name] = line.split('\t')

        return {
          name,
          isRemote: name.startsWith('origin/'),
          // origin/HEAD when a remote exists; otherwise the local default
          // (main/master/init.defaultBranch) so a no-remote repo still flags
          // its trunk.
          isDefault: Boolean(
            (remoteDefault && name === remoteDefault) || (!remoteDefault && localDefault && name === localDefault)
          )
        }
      })
  } catch {
    return []
  }
}

export {
  addWorktree,
  ensureGitRepo,
  listBaseBranches,
  listBranches,
  listWorktrees,
  parseWorktrees,
  removeWorktree,
  sanitizeBranch,
  switchBranch
}
