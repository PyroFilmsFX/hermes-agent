import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test, vi } from 'vitest'

import {
  _resetConductorsGhStateForTesting,
  ghRunStatus,
  gitFor,
  prQueryFor,
  repoStatus,
  resolveRenamePath,
  REVIEW_FILE_CAP,
  reviewList,
  reviewPrList,
  setRunGhForTesting
} from './git-review-ops'

const tempDirs: string[] = []

afterEach(() => {
  setRunGhForTesting(null)
  _resetConductorsGhStateForTesting()
  for (const dir of tempDirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

function makeRepo() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-git-status-'))

  tempDirs.push(dir)
  execFileSync('git', ['init', '-q'], { cwd: dir })
  execFileSync('git', ['config', 'user.email', 'hermes-test@example.com'], { cwd: dir })
  execFileSync('git', ['config', 'user.name', 'Hermes Test'], { cwd: dir })
  fs.writeFileSync(path.join(dir, 'tracked.txt'), 'tracked\n')
  execFileSync('git', ['add', 'tracked.txt'], { cwd: dir })
  execFileSync('git', ['commit', '-qm', 'initial'], { cwd: dir })

  return dir
}

test('resolveRenamePath: plain path is unchanged', () => {
  assert.equal(resolveRenamePath('src/a.ts'), 'src/a.ts')
})

test('gitFor accepts an internally resolved git binary path containing spaces', () => {
  assert.doesNotThrow(() => gitFor(process.cwd(), 'C:\\Program Files\\Git\\cmd\\git.exe'))
})

test('resolveRenamePath: simple rename resolves to the new path', () => {
  assert.equal(resolveRenamePath('old.ts => new.ts'), 'new.ts')
})

test('resolveRenamePath: brace rename resolves to the new path', () => {
  assert.equal(resolveRenamePath('src/{old => new}/file.ts'), 'src/new/file.ts')
})

test('resolveRenamePath: brace rename collapsing a segment', () => {
  assert.equal(resolveRenamePath('src/{lib => }/file.ts'), 'src/file.ts')
})

test('repoStatus reports an untracked directory without recursively listing its contents', async () => {
  const dir = makeRepo()
  const nested = path.join(dir, 'generated', 'deep')

  fs.mkdirSync(nested, { recursive: true })
  fs.writeFileSync(path.join(nested, 'large-output.txt'), 'generated\n')

  const status = await repoStatus(dir, 'git')

  assert.ok(status)
  assert.equal(status.untracked, 1)
  assert.equal(status.changed, 1)
  assert.deepEqual(
    status.files.map(file => file.path),
    ['generated/']
  )
})

test('reviewList reports an untracked directory without recursively listing its contents', async () => {
  const dir = makeRepo()
  const nested = path.join(dir, 'browser-profile', 'Default', 'Cache')

  fs.mkdirSync(nested, { recursive: true })

  for (let i = 0; i < 20; i++) {
    fs.writeFileSync(path.join(nested, `cache-${i}.bin`), 'generated\n')
  }

  const result = await reviewList(dir, 'uncommitted', null, 'git')

  assert.deepEqual(
    result.files.map(file => file.path),
    ['browser-profile/']
  )
})

test('reviewList caps the file payload returned to the renderer', async () => {
  const dir = makeRepo()

  for (let i = 0; i < REVIEW_FILE_CAP + 10; i++) {
    fs.writeFileSync(path.join(dir, `untracked-${String(i).padStart(4, '0')}.txt`), 'generated\n')
  }

  const result = await reviewList(dir, 'uncommitted', null, 'git')

  assert.equal(result.files.length, REVIEW_FILE_CAP)
})

test('prQueryFor: withChecks off matches query with flag absent', () => {
  const branches = ['feature/a', 'feature/b']
  const numbers = [42, 99]
  const baseline = prQueryFor('owner', 'repo', branches, numbers)
  const withFalse = prQueryFor('owner', 'repo', branches, numbers, false)
  assert.equal(withFalse, baseline)
  assert.ok(!baseline.includes('statusCheckRollup'))
  assert.ok(!baseline.includes('rateLimit'))
})

test('prQueryFor: withChecks on adds statusCheckRollup only to branches and rateLimit to query', () => {
  const branches = ['feature/branch-1', 'feature/branch-2']
  const numbers = [101]
  const query = prQueryFor('org', 'repo-name', branches, numbers, true)
  assert.ok(query.includes('commits(last:1){nodes{commit{statusCheckRollup{state}}}}'))
  assert.ok(query.includes('rateLimit{remaining resetAt cost}'))

  // Check branch nodes have rollup
  assert.ok(query.includes('headRefName: "feature/branch-1"'))
  assert.ok(query.includes('headRefName: "feature/branch-2"'))

  // Number node must NOT have statusCheckRollup
  const lines = query.split('\n')
  const numberLine = lines.find(l => l.includes('pullRequest(number: 101)'))
  assert.ok(numberLine)
  assert.ok(!numberLine.includes('statusCheckRollup'))
})

test('reviewPrList: parses checks_state and rate_limit when withChecks is on', async () => {
  const dir = makeRepo()

  setRunGhForTesting(async (args) => {
    assert.ok(Array.isArray(args), 'args must be an argv array')
    if (args[0] === 'repo' && args[1] === 'view') {
      return { ok: true, stdout: 'testowner/testrepo\n' }
    }
    if (args[0] === 'api' && args[1] === 'graphql') {
      return {
        ok: true,
        stdout: JSON.stringify({
          data: {
            rateLimit: {
              remaining: 4500,
              resetAt: '2026-09-29T12:00:00Z',
              cost: 1
            },
            repository: {
              b0: {
                nodes: [
                  {
                    number: 42,
                    headRefName: 'feature/ci',
                    isDraft: false,
                    isCrossRepository: false,
                    state: 'OPEN',
                    title: 'CI test PR',
                    url: 'https://github.com/testowner/testrepo/pull/42',
                    commits: {
                      nodes: [
                        {
                          commit: {
                            statusCheckRollup: {
                              state: 'SUCCESS'
                            }
                          }
                        }
                      ]
                    }
                  }
                ]
              }
            }
          }
        })
      }
    }
    return { ok: false, stdout: '' }
  })

  // 1. With withChecks = true
  const res = await reviewPrList(dir, null, ['feature/ci'], [], true)
  assert.equal(res.ghReady, true)
  assert.equal(res.prs.length, 1)
  assert.equal(res.prs[0].checks_state, 'SUCCESS')
  assert.deepEqual(res.rate_limit, {
    remaining: 4500,
    resetAt: '2026-09-29T12:00:00Z',
    cost: 1
  })

  // 2. With withChecks = false (sidebar pattern: byte-identical shape without checks_state / rate_limit)
  const resOff = await reviewPrList(dir, null, ['feature/ci'], [], false)
  assert.equal(resOff.ghReady, true)
  assert.equal(resOff.prs.length, 1)
  assert.equal('checks_state' in resOff.prs[0], false)
  assert.equal('rate_limit' in resOff, false)
})

test('remaining < 200 suspends Conductors gh reads until resetAt', async () => {
  const dir = makeRepo()
  let ghCallCount = 0

  const resetIso = new Date(Date.now() + 50_000).toISOString()

  setRunGhForTesting(async (args) => {
    ghCallCount++
    if (args[0] === 'repo' && args[1] === 'view') {
      return { ok: true, stdout: 'testowner/testrepo\n' }
    }
    if (args[0] === 'api' && args[1] === 'graphql') {
      return {
        ok: true,
        stdout: JSON.stringify({
          data: {
            rateLimit: {
              remaining: 150,
              resetAt: resetIso,
              cost: 1
            },
            repository: {}
          }
        })
      }
    }
    return { ok: false, stdout: '' }
  })

  const first = await reviewPrList(dir, null, ['feature/a'], [], true)
  assert.equal(first.ghReady, true)
  assert.equal(first.rate_limit?.remaining, 150)
  const callsAfterFirst = ghCallCount

  // Further Conductors PR reads should be suspended until resetAt
  const second = await reviewPrList(dir, null, ['feature/a'], [], true)
  assert.equal(second.suspended, true)
  assert.equal(ghCallCount, callsAfterFirst) // No new gh calls made!

  // ghRunStatus should also be suspended until resetAt
  const runRes = await ghRunStatus('testowner/testrepo', 555)
  assert.equal(runRes.status, 'unknown')
  assert.equal(runRes.error, 'rate_limited')
  assert.equal(ghCallCount, callsAfterFirst) // Still no new gh calls made!
})

test('per-repo failure backoff sequence 1/2/5/15 min', async () => {
  vi.useFakeTimers()
  try {
    const dir = makeRepo()
    let shouldFail = true
    let apiCallCount = 0

    setRunGhForTesting(async (args) => {
      if (args[0] === 'repo' && args[1] === 'view') {
        return { ok: true, stdout: 'testowner/testrepo\n' }
      }
      if (args[0] === 'api' && args[1] === 'graphql') {
        apiCallCount++
        if (shouldFail) {
          return { ok: false, stdout: '', stderr: '500 Internal Server Error' }
        }
        return { ok: true, stdout: JSON.stringify({ data: { repository: {} } }) }
      }
      return { ok: false, stdout: '' }
    })

    // Failure 1: triggers 1 min (60_000 ms) backoff
    await reviewPrList(dir, null, ['feature/a'], [], true)
    assert.equal(apiCallCount, 1)

    // At 30s: still backed off
    vi.advanceTimersByTime(30_000)
    let retry = await reviewPrList(dir, null, ['feature/a'], [], true)
    assert.equal(retry.error, 'backoff')
    assert.equal(apiCallCount, 1)

    // At 61s (total): 1 min backoff expired, call 2 allowed, fails -> Failure 2: 2 min backoff
    vi.advanceTimersByTime(31_000)
    await reviewPrList(dir, null, ['feature/a'], [], true)
    assert.equal(apiCallCount, 2)

    // At 60s into failure 2: still backed off
    vi.advanceTimersByTime(60_000)
    retry = await reviewPrList(dir, null, ['feature/a'], [], true)
    assert.equal(retry.error, 'backoff')
    assert.equal(apiCallCount, 2)

    // At 121s (total +61s): 2 min backoff expired, call 3 allowed, fails -> Failure 3: 5 min backoff
    vi.advanceTimersByTime(61_000)
    await reviewPrList(dir, null, ['feature/a'], [], true)
    assert.equal(apiCallCount, 3)

    // At 180s into failure 3: still backed off
    vi.advanceTimersByTime(180_000)
    retry = await reviewPrList(dir, null, ['feature/a'], [], true)
    assert.equal(retry.error, 'backoff')
    assert.equal(apiCallCount, 3)

    // At 301s (total +121s): 5 min backoff expired, call 4 allowed, fails -> Failure 4: 15 min backoff
    vi.advanceTimersByTime(121_000)
    await reviewPrList(dir, null, ['feature/a'], [], true)
    assert.equal(apiCallCount, 4)

    // At 600s into failure 4: still backed off
    vi.advanceTimersByTime(600_000)
    retry = await reviewPrList(dir, null, ['feature/a'], [], true)
    assert.equal(retry.error, 'backoff')
    assert.equal(apiCallCount, 4)

    // At 901s (total +301s): 15 min backoff expired, call 5 succeeds -> resets backoff!
    vi.advanceTimersByTime(301_000)
    shouldFail = false
    const successRes = await reviewPrList(dir, null, ['feature/a'], [], true)
    assert.equal(successRes.ghReady, true)
    assert.equal(apiCallCount, 5)

    // Immediate next call is allowed (not backed off) because success reset failures
    await reviewPrList(dir, null, ['feature/a'], [], true)
    assert.equal(apiCallCount, 6)
  } finally {
    vi.useRealTimers()
  }
})

test('ghRunStatus: caches for 60 s and enforces global 4/min cap', async () => {
  let calls = 0
  const queriedRuns: string[] = []

  setRunGhForTesting(async (args) => {
    assert.ok(Array.isArray(args), 'must be argv array')
    calls++
    const runId = args[1].split('/').pop()
    queriedRuns.push(runId)
    return { ok: true, stdout: 'completed\nsuccess\n' }
  })

  // Call 1 for run 101
  const r1 = await ghRunStatus('org/repo', '101')
  assert.deepEqual(r1, { status: 'completed', conclusion: 'success' })
  assert.equal(calls, 1)

  // Call 2 for run 101 immediately: hits 60s cache
  const r1Cached = await ghRunStatus('org/repo', '101')
  assert.deepEqual(r1Cached, { status: 'completed', conclusion: 'success' })
  assert.equal(calls, 1)

  // Call for run 102, 103, 104 (total 4 distinct calls made to gh in window)
  await ghRunStatus('org/repo', '102')
  await ghRunStatus('org/repo', '103')
  await ghRunStatus('org/repo', '104')
  assert.equal(calls, 4)

  // 5th call for a new run 105: hits 4/min cap! Returns unknown without calling gh
  const r5 = await ghRunStatus('org/repo', '105')
  assert.equal(r5.status, 'unknown')
  assert.equal(r5.conclusion, null)
  assert.equal(calls, 4) // No new call!

  // 5th call for an ALREADY cached run (e.g. 101): returns cached value even when capped
  const rCachedUnderCap = await ghRunStatus('org/repo', '101')
  assert.deepEqual(rCachedUnderCap, { status: 'completed', conclusion: 'success' })
  assert.equal(calls, 4)
})

test('gh missing or unauthenticated returns typed gh_unavailable result', async () => {
  const dir = makeRepo()

  setRunGhForTesting(async () => {
    return { ok: false, stdout: '', stderr: 'gh: command not found', error: Object.assign(new Error('ENOENT'), { code: 'ENOENT' }) }
  })

  // reviewPrList with withChecks: true
  const prRes = await reviewPrList(dir, null, ['feature/a'], [], true)
  assert.equal(prRes.ghReady, false)
  assert.equal(prRes.error, 'gh_unavailable')
  assert.equal(prRes.gh_unavailable, true)

  // ghRunStatus
  const statusRes = await ghRunStatus('owner/repo', 999)
  assert.equal(statusRes.status, 'gh_unavailable')
  assert.equal(statusRes.conclusion, null)
  assert.equal(statusRes.error, 'gh_unavailable')
  assert.equal(statusRes.gh_unavailable, true)
})

test('ghRunStatus and reviewPrList pass argv arrays only to runGh', async () => {
  const dir = makeRepo()
  const recordedArgvs: any[] = []

  setRunGhForTesting(async (args) => {
    recordedArgvs.push(args)
    if (args[0] === 'repo' && args[1] === 'view') {
      return { ok: true, stdout: 'owner/repo\n' }
    }
    if (args[0] === 'api' && args[1] === 'graphql') {
      return { ok: true, stdout: JSON.stringify({ data: { repository: {} } }) }
    }
    if (args[0] === 'api' && args[1].includes('actions/runs')) {
      return { ok: true, stdout: 'completed\nsuccess\n' }
    }
    return { ok: true, stdout: '' }
  })

  await reviewPrList(dir, null, ['feat'], [], true)
  await ghRunStatus('owner/repo', 123)

  assert.ok(recordedArgvs.length >= 2)
  for (const argv of recordedArgvs) {
    assert.ok(Array.isArray(argv), 'Must be an Array')
    for (const arg of argv) {
      assert.equal(typeof arg, 'string')
    }
  }
})

test('ghRunStatus refuses run ids and repo slugs that could reshape the gh api path', async () => {
  _resetConductorsGhStateForTesting()
  const runRequests: string[][] = []
  setRunGhForTesting(async args => {
    if (args.some(arg => arg.includes('actions/runs'))) {
      runRequests.push(args)
    }
    return { ok: true, stdout: 'completed\nsuccess\n' }
  })

  for (const runId of ['../../user', '1?per_page=100', '12/jobs', '', '-5', 'abc']) {
    const res = await ghRunStatus('org/repo', runId)
    assert.equal(res.error, 'invalid_run_id', `run id ${JSON.stringify(runId)}`)
  }

  for (const repo of ['org/..', 'org/repo?x=1', 'o rg/repo']) {
    const res = await ghRunStatus(repo, '101')
    assert.notEqual(res.status, 'completed', `repo ${JSON.stringify(repo)}`)
  }

  // A non-slug repo may be looked up as a path (`gh repo view`), but no run request goes out.
  assert.deepEqual(runRequests, [])
})
