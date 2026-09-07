# Fork inventory — what cntrl changed in Hermes, and how we keep up with upstream

Last audit: 2026-09-07. Branch `cntrl-hermes`. Upstream `origin/main`.

## 1. Shape of the carry

| layer | commits | lives in | merge cost |
|---|---|---|---|
| Claude Agent SDK runtime | ~59 | upstream PR #65982 (author `fcavalcantirj`) | high until the PR merges. Not ours. |
| cntrl additions on the SDK runtime | 6 | core files, small hunks | low, listed in §3 |
| cntrl plugins + docs | 9 | `cntrl-plugins/`, `docs/cntrl/`, `CNTRL-HERMES.md` | none |

The PR author rebases #65982 onto fresh main every few days and squashes it
(20 commits on 2026-09-05, rebased again 2026-09-07). Every SDK fix we carried
(turn watchdog, background-result delivery, approval tool_use_id, setting_sources,
max_budget_usd, stream-death retire) is present in the PR head by content. The
four follow-ups from `fcavalcantirj/hermes-agent#3` (picker entry, background
review restore, one-shot join, startup tracebacks) are folded in too, except the
self-improve review routing which we still carry (see §3).

## 2. Two ways to sync, and which we use

- **Merge** `origin/main` into our branch. One commit, one conflict round.
  History keeps the merge points. This is what we do.
- **Rebase** our commits onto main. Cleaner log but every carried commit can
  conflict again. At 10k commits behind it is not worth it.

Cheaper than either when #65982 is fresh: branch from the PR head
(`git fetch origin pull/65982/head`), cherry-pick only the cntrl commits (§3, §4).
The PR head is usually under 100 commits behind main.

Cadence target: merge upstream at least weekly. Smaller gap, smaller conflicts.

## 3. Core edits we own (cntrl-only, on top of the SDK runtime)

Each line is one reason to touch a core file. Keep our hunk on conflict unless
upstream moved the surrounding code.

| file | what | why |
|---|---|---|
| `agent/claude_sdk_runtime.py` | `_SDK_SKILLS_GUIDANCE` one-sentence skill guidance | the native `SKILLS_GUIDANCE` block trips Anthropic's harness screen (400 "out of extra usage") |
| `agent/claude_sdk_runtime.py`, `agent/transports/claude_agent_sdk_session.py` | `agent.claude_agent_sdk.plugins` list, passed as `--plugin-dir` | load conductor into SDK sessions with `setting_sources: []` kept (isolation, decided 2026-09-01) |
| `agent/transports/claude_agent_sdk_session.py` | `agent.claude_agent_sdk.cli_path` | pin the SDK to `~/.local/bin/claude` so it follows `claude update` |
| `agent/transports/hermes_tools_mcp_server.py` | `--profile` support, `skill_manage` served on the claude-agent-sdk profile | skill auto-capture silently dropped on the SDK runtime |
| `hermes_cli/models.py` | `claude-fable-5-1` first in the Anthropic catalog | SDK picker |
| `hermes_cli/config_defaults.py`, `hermes_cli/inventory.py` | defaults for `plugins`, `cli_path` | config-only flags |
| `pyproject.toml`, `uv.lock`, `tools/lazy_deps.py` | `claude-agent-sdk==0.2.150`, exempt from the 14-day quarantine | 0.2.120 bundled a CLI that 400s on Fable 5.1 |
| `run_agent.py` | self-improve review routed off the SDK runtime | one-shot runs killed the review at exit |
| `.gitignore` | `.hermes-test/` | test home |

## 4. Out-of-tree (zero merge cost)

- `cntrl-plugins/cntrl_sync/` — one-way cntrl ↔ Hermes kanban sync, v0. Docs in
  `docs/cntrl/kanban-one-way-sync.md`.
- `$HERMES_HOME/plugins/pgvector/` — memory provider (`hermes-memory-pgvector`),
  not in the repo, points at cntrl Postgres `:5434/thinkscore`.
- conductor — loaded via `agent.claude_agent_sdk.plugins`, lives in
  `~/.claude/plugins/marketplaces/…`.
- `docs/councils/council-log.jsonl`, `CNTRL-HERMES.md`, `docs/cntrl/`.

## 5. Merge procedure

```bash
git fetch origin
git worktree add ../hermes-merge cntrl-hermes     # optional: keep main tree clean
git merge origin/main
# resolve. Files in §3 keep our hunks. Everything else takes upstream.
uv sync --extra claude-agent-sdk
.venv/bin/python -m pytest tests/agent/test_claude_sdk_runtime.py cntrl-plugins/cntrl_sync -q
# smoke: one real SDK turn in the desktop (CNTRL-HERMES.md, "Desktop dev launch")
git commit
```

Known pre-existing failures: two `TestSystemPromptAppend` tests in
`tests/agent/test_claude_sdk_runtime.py` (branch divergence, not ours).

## 6. Rules for teammates on their own branches

1. Branch from `cntrl-hermes`, not `main`.
2. Put your feature in `cntrl-plugins/<name>/` or `$HERMES_HOME/plugins/`.
   Ask before editing any file under `agent/`, `hermes_cli/`, `gateway/`.
3. Merge `cntrl-hermes` into your branch every day. Never rebase a shared branch.
4. If you must edit core, add a row to §3 in the same commit.

## 7. Open items to verify on the SDK runtime (tracked here, not done)

- **Session-to-session messaging.** Claude Code can list and message other
  local Claude sessions (`ListAgents` / `SendMessage`). Not yet checked whether
  sessions spawned by the SDK (`CLAUDE_CODE_ENTRYPOINT=sdk-py`) register as
  discoverable, or can see each other. Goal: a host session that routes work to
  the right session. Test: two SDK sessions in the desktop, one lists agents.
- **MCP parity inside the SDK runtime.** Hermes' own MCP layer has `/reload-mcp`
  (live add, no restart). The SDK session's MCP list (`mcp_servers`, plugins) is
  fixed at session start. To add one mid-session the runtime must rebuild the
  SDK client with the resume id. Proven so far: `tb-workers` registers and
  answers a tool call (2026-09-01). Not proven: live add, `hermes mcp add`
  servers reaching the SDK session, OAuth MCP servers.
- **Session groups / projects.** Hermes has profiles (one `HERMES_HOME` each)
  and sessions. No grouping of sessions into projects. Candidate seam: an
  out-of-tree plugin plus a desktop tab filter. Not started.
