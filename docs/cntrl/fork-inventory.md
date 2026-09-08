# Fork inventory — what cntrl changed in Hermes, and how we keep up with upstream

Last audit: 2026-09-07 (rebuilt on the PR #65982 head that day). Branch `cntrl-hermes`. Upstream `origin/main`.
Machine-derived snapshot with dates: `fork-inventory.generated.md`; run history: `fork-inventory-log.md`.

## 1. Shape of the carry

| layer | commits | lives in | merge cost |
|---|---|---|---|
| Claude Agent SDK runtime | 20 (the PR head itself) | upstream PR #65982 (author `fcavalcantirj`) | none for us: we sit ON the PR head. Not ours. |
| cntrl additions on the SDK runtime | 5 | core files, small hunks | low, listed in §3 |
| cntrl plugins + docs | 11 | `cntrl-plugins/`, `docs/cntrl/`, `CNTRL-HERMES.md`, `CLAUDE.md` | none |

Before 2026-09-07 the branch was 10,458 commits behind main and carried 74
commits (a stale copy of the PR plus ours). It was rebuilt that day: branch from
the PR head, re-apply only the cntrl commits. Three carried hunks were dropped
because the PR head already had them (routed review, startup traceback, picker
self-auth); the old branch is kept as `cntrl-hermes-pre-sep7`.

The PR author rebases #65982 onto fresh main every few days and squashes it
(20 commits on 2026-09-05, rebased again 2026-09-07). Every SDK fix we carried
(turn watchdog, background-result delivery, approval tool_use_id, setting_sources,
max_budget_usd, stream-death retire) is present in the PR head by content. The
four follow-ups from `fcavalcantirj/hermes-agent#3` (picker entry, background
review restore, one-shot join, startup tracebacks) are folded in too, except the
self-improve review routing which we still carry (see §3).

## 2. Two ways to sync, and which we use

- **Rebuild on the PR head (what we do while #65982 is open).** The author
  rebases the PR onto fresh main every few days. We fetch its head and rebase
  only our ~16 commits onto it. Conflicts touch only §3 files.
- **Merge** `origin/main` into our branch. One commit, one conflict round.
  Use this once the PR merges, or if the PR goes stale.
- **Rebase** our commits onto main directly. Not worth it while the PR is open;
  the SDK runtime would conflict on every sync.

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
| `hermes_cli/models_catalog_static.py`, `agent/model_metadata.py` | `claude-fable-5-1` first in the Anthropic catalog + 1M context | SDK picker (catalog moved out of `hermes_cli/models.py` upstream) |
| `agent/transports/hermes_tool_exposure.py` | `CLAUDE_AGENT_SDK_SKILL_TOOLS = ("skill_manage",)` on the SDK profile | skill writer for auto-capture; profile machinery lives here upstream now |
| `tests/agent/test_claude_sdk_runtime.py`, `tests/agent/transports/test_hermes_tools_mcp_server.py` | tests for cli_path, plugins, skill guidance, warn-once | pin the rows above |
| `website/docs/user-guide/features/claude-agent-sdk-runtime.md` | cli_path, plugins, review routing paragraphs | user docs for the rows above |
| `hermes_cli/config_defaults.py`, `hermes_cli/inventory.py` | defaults for `plugins`, `cli_path` | config-only flags |
| `pyproject.toml`, `uv.lock`, `tools/lazy_deps.py` | `claude-agent-sdk==0.2.150`, exempt from the 14-day quarantine | 0.2.120 bundled a CLI that 400s on Fable 5.1 |
| `run_agent.py` | self-improve review routed off the SDK runtime | one-shot runs killed the review at exit |
| `.gitignore` | `.hermes-test/` | test home |
| `tools/mcp_tool_agent.py`, `hermes_cli/cli_info_mixin.py`, `gateway/run_turn.py`, `tui_gateway/methods_tools.py` | `sdk_rotate=` on `refresh_agent_mcp_tools` + `_maybe_rotate_sdk_session`; explicit `/reload-mcp` callers pass it | live MCP reload for SDK sessions |
| `agent/claude_sdk_runtime.py` | `rotate_claude_sdk_session` + in-memory resume stash in `_persisted_sdk_session_id` | same |
| `tests/tools/test_refresh_agent_mcp_tools.py` | rotation hook test | pins the row above |

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
git fetch origin pull/65982/head:pr-65982-new
OLD=$(git merge-base cntrl-hermes pr-65982-sep7)      # the PR head we sit on now
git rebase --onto pr-65982-new $OLD cntrl-hermes       # only our commits move
# resolve. Files in §3 keep our hunks. Everything else takes upstream.
# If the PR is closed/merged instead: git merge origin/main.
uv sync --extra claude-agent-sdk --inexact             # --inexact keeps psycopg
.venv/bin/python -m pytest tests/agent/test_claude_sdk_runtime.py cntrl-plugins/cntrl_sync -q
# smoke: one real SDK turn in the desktop (CNTRL-HERMES.md, "Desktop dev launch")
git commit
.venv/bin/python scripts/cntrl/fork_inventory.py --check   # then commit its outputs
```

Gateway suite 2026-09-07 (7,817 pass / 16 fail): 9 of the 16 fail identically
on the pristine PR head; the other 7 (discord send, multi-image, session-store
prune, teams dotenv, telegram polling) pass in isolation on our branch and are
order-dependent flakes. None touch the carry. Not ours.

Known: a headless one-shot (`hermes chat -q`) that calls a tool waits the full
300 s approval timeout with nobody to answer; not a runtime fault. The title
auxiliary lane once produced a `{"title` fragment as the session title (seen
2026-09-07 via gemini aux); harmless, not investigated.

## 6. Rules for teammates on their own branches

1. Branch from `cntrl-hermes`, not `main`.
2. Put your feature in `cntrl-plugins/<name>/` or `$HERMES_HOME/plugins/`.
   Ask before editing any file under `agent/`, `hermes_cli/`, `gateway/`.
3. Merge `cntrl-hermes` into your branch every day. Never rebase a shared branch.
4. If you must edit core, add a row to §3 in the same commit.

## 7. Open items to verify on the SDK runtime (tracked here, not done)

- **Session-to-session messaging — PROVEN 2026-09-07, both ways.** `ListAgents`
  and `SendMessage` are Claude Code CLI tools, so they ride the pinned `cli_path`
  binary (2.1.263). Probes in `.sdkprobe/` (`setting_sources=[]`): an SDK session
  inside Hermes listed 12 live sessions on the machine, then sent a message to
  the interactive Claude Code session that was driving this work; it arrived as
  a cross-session message. Goal stands: a host session in Hermes that routes
  work to the right session. Not yet done: Hermes sessions naming themselves so
  others can find them.
- **Live MCP reload inside the SDK runtime — DONE 2026-09-07.** The SDK's CLI
  holds its MCP/plugin list from process start, so `/reload-mcp` (CLI, gateway,
  TUI RPC) now rotates the live SDK session: `rotate_claude_sdk_session` closes
  the CLI, stashes the resume id, and the next turn rebuilds with fresh
  `mcp_servers` / `plugins` / `cli_path` and resumes the conversation. A tool
  surface change on any refresh path rotates too. Live proof
  (`.sdkprobe/rotate_probe.py`): two real turns, new CLI pid between them, the
  second turn recalled the first. Still true: `hermes mcp add` servers reach
  the SDK session only with `hybrid_mcp_bridge: true`; without it the SDK sees
  hermes-tools + `plugins:` only.
- **Host router — DONE 2026-09-07.** Sessions are now named
  (`agent.claude_agent_sdk.session_name`, default `hermes:{title}`), so
  `ListAgents` / `SendMessage` can address a Hermes session. The map from work
  to owner is out-of-tree in `cntrl-plugins/cntrl_router/`: a JSON registry in
  `$HERMES_HOME/cntrl-routes.json`, a small CLI (`router.py list|add|find`),
  and a Hermes skill that teaches the protocol (read registry, verify the peer
  is live, hand off once, never guess). Live-proven: a host session matched
  "the upstream sync is behind", correctly picked `hermes-fork` over `kanban`,
  checked liveness, and the message arrived in the target session.

- **Session groups / projects.** Hermes has profiles (one `HERMES_HOME` each)
  and sessions. No grouping of sessions into projects. Candidate seam: an
  out-of-tree plugin plus a desktop tab filter. Not started.
