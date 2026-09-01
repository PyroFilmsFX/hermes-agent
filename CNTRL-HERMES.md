# cntrl × Hermes — integration shape

Working notes on branch `cntrl-hermes`. Everything marked **proven** was executed and
observed in this repo; everything marked **open** is a decision or an untested assumption.

---

## 1. The core decision: don't merge the codebases

Three hard blockers make "package Hermes inside cntrl" a rewrite rather than a packaging job:

| | cntrl | Hermes |
|---|---|---|
| storage | PostgreSQL + pgvector | SQLite (42 modules `import sqlite3`) |
| kanban | bizops board | its own board, 14,197 LOC, SQLite |
| auth | SSO + workspaces | `dashboard_auth` provider framework |

`asyncpg` appears in Hermes only in the Matrix *chat* adapter — never in storage.
Upstream issue **#38185** ("The kanban module is hardcoded to SQLite only!") confirms this
is a known, unfixed constraint, not something we misread.

So the integration is **cntrl as control plane, Hermes as engine**, joined at three plugin
seams — all of which are out-of-tree and therefore carry no merge tax:

| seam | buys | tax |
|---|---|---|
| `$HERMES_HOME/plugins/<name>/` memory provider | memory in cntrl Postgres | none |
| `plugins/dashboard_auth/` | cntrl SSO logs into Hermes | none |
| profiles + process supervision | per-user isolation | none (ops) |

This is also the shape Nous themselves ship: the bundled `nous` dashboard-auth provider
encodes `client_id: agent:{agent_instance_id}`, provisioned per instance at Fly.io deploy
time. Instance-per-account, not one multi-tenant process.

---

## 2. Memory → cntrl Postgres — **proven seam**

`plugins/memory/` is a real `MemoryProvider` ABC with four discovery sources. Two are
out-of-tree:

```
1. bundled          plugins/memory/<name>/
2. user-installed   $HERMES_HOME/plugins/<name>/     ← no fork
3. project-local    ./.hermes/plugins/<name>/
4. pip entry point  hermes_agent.memory_providers    ← no fork
```

Only one provider is active at a time, selected by `memory.provider`.

**Proven:** dropped `hermes-memory-pgvector` (BSD-3, upstream issue #29537) into
`.hermes-test/plugins/pgvector/` and Hermes discovered it with **zero source changes**.
It reports `is_available=False` only because `psycopg` isn't installed and the DB is down.

That provider is a strong starting point rather than something to write from scratch:
Postgres + pgvector with HNSW, hybrid vector + `tsvector` full-text (RRF) ranking, an async
writer with a connection pool, agent attribution and parent→child delegation provenance
(`memory_agents` / `memory_agent_edges`), conversation capture, and per-agent themes via an
`X-Hermes-Session-Key` header. Its own note confirms the direction: *"in-tree memory-provider
directory is closed; new providers install into `~/.hermes/plugins/`."*

**Two things it needs before it runs (open):**
1. The cntrl dev Postgres — `:5434/thinkscore` was refusing connections during this session.
2. A 768-dim embedding endpoint (OpenAI-compatible or Ollama). cntrl already runs embedding
   workers; pointing the provider at those is likely cheaper than standing up Ollama.

**Decision to make:** use `pgvector`'s own tables (`memory_entries`, …) as-is, or fork it into
a `cntrl` provider that writes cntrl's existing memory schema so cntrl's own memory UI can
read it. Start with as-is to prove the loop; fork only if the UI needs it.

---

## 3. Kanban — two boards on purpose

Hermes' board is not a gap to fill; it's a duplicate to arbitrate.
`kanban_db.py` (11.7k LOC) + `kanban.py`, `kanban_swarm.py`, `kanban_decompose.py`,
`kanban_specify.py`, `kanban_diagnostics.py`, plus agent-facing `tools/kanban_tools.py`.
Tables: `task_events`, `task_comments`, `task_runs`, `kanban_notify_subs`.

They are different products and should stay that way:

- **cntrl board = bizops.** Clients, deals, invoices, human assignees. Broad purpose. Authoritative.
- **Hermes board = agent work queue.** Dev tasks the agent decomposes, swarms, and runs
  (`kanban_swarm`, `task_runs`). Per-user, per-profile, disposable.

**Sync is one-way and narrow:** a cntrl dev task assigned to a user is pushed into *that
user's* Hermes board; status flows back. Bizops tasks never go to Hermes — there's no reason
for the agent to see an invoice. Anything richer than one-way status is scope creep.

---

## 4. Multi-user — per-user instance, and why gating alone fails

`hermes_cli/profiles.py` opens with: *"Each profile is a fully independent HERMES_HOME
directory with its own …"*. That is the isolation primitive.

**Session-gating inside one Hermes does not isolate users.** All state — `memories/`,
`state.db`, the kanban, sessions — lives under one `HERMES_HOME`. One process means one
shared memory and one shared board for everyone. Gating which session a user may *join*
doesn't change what they share underneath.

So: **profile (or container) per user**, provisioned on first login, exactly as intended.

One nuance found in `hermes dashboard --help`: by default *"profile launches attach to (or
start) ONE machine-level server and preselect the profile"*, while `--isolated` gives a
profile its own server. So there are two deployment shapes:

| shape | isolation | cost |
|---|---|---|
| machine-level server, many profiles | filesystem/state per profile | one process |
| `--isolated` server per profile | process too | N processes |

**Open, and load-bearing:** whether the machine-level server safely multiplexes profiles under
concurrent load. Untested. If it does, multi-tenancy is much cheaper than container-per-user.
Test this before committing to a shape.

---

## 5. UI shape inside cntrl

The desktop is an Electron shell over a FastAPI backend + a React SPA; cntrl can host the same
surfaces without Electron.

- **Dedicated Hermes tab** — the full workspace (sessions, capabilities, artifacts, scheduled
  jobs, its own kanban) for power use. Cheapest path: embed the existing SPA against the
  per-user backend.
- **Popped-out single session** — one session rendered inside a cntrl page. The backend already
  supports this: `/api/sessions/{id}/events/stream` is per-session, so a cntrl panel can stream
  one session without the Hermes chrome. This is the natural fit for host/claw/workspace
  join-only sessions, where the user should see *one* conversation, not the whole app.
- **Tabbed sessions** — cntrl's existing tab system owns the tabs; each tab is a session id.
  Hermes stays the engine; cntrl owns navigation, so `tabToPath()` routing rules still apply.

The split that keeps this sane: **cntrl owns identity, navigation, and bizops; Hermes owns the
agent loop, its dev board, and session state.** Anything that needs to be in both crosses via
the narrow sync in §3.

---

## 6. Status

**Proven in this session**
- Claude Agent SDK runtime runs real turns on the subscription (tools, file writes, no API key)
- Self-improve loop restored when the review is routed off the SDK runtime
- One-shot runs now complete the review instead of killing it at exit
- `claude-agent-sdk` selectable in the model picker (12 models)
- Out-of-tree memory provider discovered with zero source changes

**Open**
- cntrl Postgres + embedding endpoint for the memory provider
- Machine-level server vs `--isolated` under concurrent load
- Whether to fork `pgvector` into a cntrl-schema provider
- Per-user resource cost at scale

**Upstream**
- 4 fixes proposed: https://github.com/fcavalcantirj/hermes-agent/pull/3

---

## 7. Rulings and status — 2026-09-01

**Decided (Aug 26 four-model council, unanimous; ledger: `docs/councils/council-log.jsonl`):**
Hermes is the spine. cntrl is the control plane. No codebase merge. All our code lives
out-of-tree in `$HERMES_HOME/plugins/`. conductor stays independent for now (3/4); revisit
after the first milestone.

**Branch state:** the 7 `fix/claude-sdk-parity-followups` commits are carried on
`cntrl-hermes` as one commit (same bytes; that branch is a rebase onto a newer upstream and
cannot fast-forward here).

**Found on resume (Sep 1):**
- The SDK path **worked**: 47 SDK sessions and 9 completed desktop turns between Aug 14 and
  Aug 16 01:51 (`.hermes-test/logs/agent.log`). Streaming deltas arrived from the SDK, but
  22 of 26 were logged `callback=NONE` — only the CLI sink was wired, not the desktop's
  `_stream_callback`. That is the "UI streaming not working" bug. The fan-out fix (last of
  the 7 carried commits, Aug 16 02:06) is **untested in the desktop**: the 14:33 retest hit
  "claude-agent-sdk is not installed".
- `claude-agent-sdk` is an optional extra. It vanished from `.venv` between Aug 16 10:00 and
  14:33, and `.venv` was rebuilt again Aug 26 13:12. Any `uv sync` (and a bare `uv run`,
  which syncs first) without `--extra claude-agent-sdk` removes it. The desktop runs this
  repo's `.venv` (`findPythonForRoot` in `apps/desktop/electron/main.ts`). Reinstall with
  `uv sync --extra claude-agent-sdk`; afterwards use `.venv/bin/python` or `uv run --no-sync`.
- `psycopg` is not installed either; the pgvector provider stays `is_available=False` until
  `psycopg[binary,pool]` + `pgvector` are in `.venv`. Postgres `:5434` is up now.
- Desktop dev needs `npm ci` at repo root (no `node_modules` anywhere).
- Test home: `HERMES_HOME=$PWD/.hermes-test` (provider `claude-agent-sdk`, streaming on,
  `setting_sources: ["user"]`, memory `pgvector`). Embedding key is read from
  `HERMES_EMBED_API_KEY`; `GEMINI_API_KEY` is set in the shell, the Hermes name is not.

**Done Sep 1 (all proven on the running desktop, `HERMES_HOME=.hermes-test`):**
- Streaming reaches the desktop. A real turn on `claude-fable-5-1` grew the assistant message
  3 → 33 → 150 → 303 → 592 → 732 chars over ~6 s (sampled over CDP every second). Item (1) closed.
- `claude-fable-5-1` is first in the claude-agent-sdk picker (`_PROVIDER_MODELS["anthropic"]`).
- `claude-agent-sdk` 0.2.150 (bundles Claude Code 2.1.257); the package is exempt from the uv
  14-day quarantine (`exclude-newer-package`). 0.2.120 bundled 2.1.211, which 400s on Fable 5.1.
- `agent.claude_agent_sdk.cli_path` pins the SDK to `~/.local/bin/claude` so it follows
  `claude update`; set in the test home.
- Desktop dev launch (from repo root):
  `HERMES_HOME=$PWD/.hermes-test HERMES_DESKTOP_HERMES_ROOT=$PWD HERMES_DESKTOP_CDP_PORT=9223 npm run dev --workspace apps/desktop`
  (9222 is held by another browser on this machine). `apps/desktop/scripts/live-drive.mjs`
  with `CDP_PORT=9223` sends prompts and evals the DOM.
- "Transcribing 0:00" that looked stuck was the first whisper model load + HuggingFace check
  (15:44:27 → 15:45:50, ~83 s) with no progress UI; it then returned an empty transcript. Not a
  hang. A cancel affordance / "loading speech model" state would be the fix if it matters.
- Known: two `TestSystemPromptAppend` tests in `tests/agent/test_claude_sdk_runtime.py` fail
  from branch divergence (session_search tool, skills index). Pre-existing, not yet ported.

- pgvector was already proven Aug 15–16: 3 `memory_entries` (all embedded), 29 captured
  `conversations` in `:5434/thinkscore`. It looked dead on Sep 1 only because the Aug 26 venv
  rebuild dropped `psycopg` too. Restored (`psycopg[binary]`, `psycopg-pool`), embed key now in
  `.hermes-test/.env` (`HERMES_EMBED_API_KEY`), provider registered + activated on a real turn,
  conversations 29 → 31. Same trap as the SDK: a bare `uv sync` removes psycopg again.

**Item 5 decided Sep 1 — conductor inside Hermes, option B.** `setting_sources: ["user"]`
(option A) pulled the whole `~/.claude` into every Hermes turn: 30 enabled plugins, the
agent-deck + orca session hooks on 15 events, context7/kitty MCP servers spawned per turn, and
the permission allowlist underneath Hermes' posture (seen live: `apps/desktop/.claude/state`
appeared at 15:44 from those hooks). Option B keeps `setting_sources: []` and loads conductor
explicitly via the new `agent.claude_agent_sdk.plugins` (SDK `--plugin-dir`). Proven: CLI argv
carries `--plugin-dir …/conductor`, only the hermes-tools MCP is spawned, conductor's own hooks
fire (telemetry written in the session cwd). Flip back to A by setting `setting_sources: ["user"]`.
Residual: conductor's hooks write `.claude/state` into the SDK session cwd; the `tb-workers` MCP
server is registered but fails to connect there exactly as it does in Claude Code today.

**Anthropic third-party-harness screen (found Sep 1, cost two hours).** Agent-SDK requests
(`CLAUDE_CODE_ENTRYPOINT=sdk-py`) whose appended system prompt reads too much like another
agent product are rejected with a misleading `400 invalid_request_error: You're out of extra
usage` — no balance is involved; the interactive CLI accepts the identical prompt. Cache-hit
requests slip through, so it surfaces only when the append changes. Hermes' append sits close
to the line: adding the native `SKILLS_GUIDANCE` block tipped every request over; a one-sentence
skill guidance (`_SDK_SKILLS_GUIDANCE`) passes. Before adding prose to `build_system_prompt_append`,
probe: build the append, send "say ok" through `ClaudeSDKClient` with `setting_sources=[]`
(script shape in `agent/claude_sdk_runtime.py` next to the constant). Request ids for support:
`req_011CedUBbtgdDtsbubjbawEg`.

**Item 3 done Sep 1 — skill auto-capture on the SDK runtime.** Ported the hermes-tools
`--profile` support (the runtime called it, the branch lacked it → skills index silently dropped,
read_file/search_files never registered); the claude-agent-sdk profile now serves
`skill_manage`; the unrouted review skips with one warning instead of silence.

**Item 4 done Sep 1 — kanban one-way sync v0.** Proposal + mapping in
`docs/cntrl/kanban-one-way-sync.md`; code out-of-tree in `cntrl-plugins/cntrl_sync/` (symlinked
into `$HERMES_HOME/plugins/`, opt-in via `plugins.enabled`). Proven live against cntrl
(`thinks-mini`, :3101) and the real board: labelled task → Hermes (`triage`/`ready`), completed in
Hermes → cntrl `done` + result comment, second pull no echo. v1 (cntrl webhooks for
`task.assigned`/`task.status_changed`, `metadata.hermes_task_id`) is cntrl-side work.

**Open, in order:** (1) ~~desktop UI streaming~~ done, (2) ~~pgvector against `:5434/thinkscore`~~ done,
(3) ~~skill auto system~~ done, (4) ~~kanban one-way sync~~ v0 done, (5) ~~conductor inside Hermes~~ decided (option B, above).
