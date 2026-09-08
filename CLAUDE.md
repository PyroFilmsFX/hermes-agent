# CLAUDE.md — cntrl fork of Hermes

Read `AGENTS.md` first (upstream's dev guide). This file only adds the fork rules.

## What this repo is

Upstream Hermes (`origin` = NousResearch/hermes-agent) plus a thin cntrl layer.
Branch `cntrl-hermes`. Our fork remote is `myfork`.
Full inventory of what we changed and why: `docs/cntrl/fork-inventory.md`.
Working notes and proofs: `CNTRL-HERMES.md`.

## The one rule: stay mergeable

Upstream moves ~180 commits a day. We merge upstream often. So:

1. **New code goes out-of-tree.** `cntrl-plugins/<name>/` (symlinked into
   `$HERMES_HOME/plugins/`), `$HERMES_HOME/plugins/`, or a pip entry point.
   Never a new feature inside Hermes core files.
2. **Core edits only when there is no seam.** Keep them tiny, keep them in one
   commit, and list them in `docs/cntrl/fork-inventory.md` with the reason.
3. **The Claude Agent SDK runtime is upstream PR #65982, not ours.** Do not
   fork it further. Our additions on top of it are listed in the inventory.
   When that PR merges, our carry shrinks to the inventory's "cntrl-only" list.
4. **Prefix fork commits** with `cntrl:` (docs/plugins) or `claude-sdk:`
   (additions on the SDK runtime) so `git log` separates them from upstream.
5. **Keep the inventory current.** After every upstream sync and after any
   commit that touches a core file, run `/fork-inventory` (or
   `.venv/bin/python scripts/cntrl/fork_inventory.py --check`) and commit
   the generated snapshot + log with it. An unlisted core file fails the check.

## Merging upstream (do this often)

Preferred while PR #65982 is open: rebuild on its head, it is rebased onto
fresh main every few days (`docs/cntrl/fork-inventory.md` §2 and §5).

```bash
git fetch origin
git fetch origin pull/65982/head:pr-65982-sep7
git rebase --onto pr-65982-sep7 <old-pr-base> cntrl-hermes   # our commits only
# resolve, then:
uv sync --extra claude-agent-sdk
.venv/bin/python -m pytest tests/agent/test_claude_sdk_runtime.py cntrl-plugins -q
```

Conflicts land almost only in the files listed under "core edits" in the
inventory. Keep our side for those hunks unless upstream moved the code.

## This repo is PUBLIC

`myfork` = https://github.com/PyroFilmsFX/hermes-agent, a public fork. GitHub
forks of a public repo cannot be made private. So:

- Never commit anything under `.hermes-test/`, `.sdkprobe/`, `.claude/state/`,
  `docs/telemetry/`, `worker-routing.json` (all gitignored, keep it that way).
- No tokens, no `.env`, no `config.yaml` with keys, no client names or client
  data in docs, no internal hostnames beyond what `CNTRL-HERMES.md` already
  has. Council ledgers and notes are fine; they read as engineering notes.
- Before `/tb-ship`: `git diff --name-only origin/main..HEAD | xargs grep -nE
  'sk-ant-|AIza|xoxb-|ghp_|BEGIN .*PRIVATE'` must return only fake test values.
- Anything that must stay private goes in cntrl (private repo) or in
  `$HERMES_HOME/plugins/`, not here.

## Environment traps

- **This checkout is shared with other Claude sessions.** They can and do
  change the branch under you. Before any build step, confirm
  `git rev-parse --abbrev-ref HEAD` is `cntrl-hermes`. On 2026-09-07 another
  session checked out `main` and fast-forwarded it mid-session; nothing was
  lost because our work was committed and pushed, which is the whole defence.
  Commit early, push often, and re-check the branch after any long wait.

- **The venv loses packages constantly.** `uv sync` on the wrong branch (main
  has no `claude-agent-sdk` extra) or without the extras strips the SDK,
  psycopg AND pytest — silently, so tests then fail with
  `No module named pytest` rather than anything meaningful. Full restore:
  ```bash
  uv sync --extra claude-agent-sdk --extra dev --inexact
  uv pip install "psycopg[binary]" psycopg-pool
  ```
  Check before trusting a test run:
  `.venv/bin/python -c "import pytest, claude_agent_sdk, psycopg"`
- Test home is `HERMES_HOME=$PWD/.hermes-test`.
- Desktop dev: see "Desktop dev launch" in `CNTRL-HERMES.md`.
- Anthropic screens SDK requests whose system-prompt append looks like another
  agent product. Probe before adding prose to `build_system_prompt_append`
  (see `CNTRL-HERMES.md`, "harness screen").
