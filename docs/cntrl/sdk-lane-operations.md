# SDK lane operations (Claude Agent SDK runtime in the cntrl fork)

Last updated: 2026-09-11. Companion to `CLAUDE.md` (rules) and
`docs/cntrl/fork-inventory.md` (what we changed). This file is the "how it
actually behaves and how to look inside it" notes for the Claude Agent SDK lane:
the desktop talks to a Python backend (`hermes_cli.main --profile <p> serve`),
the backend runs each turn through a `claude` CLI child process, and the CLI
does the tools.

## 1. Permission modes and the guardian (why the CPU/plan burn happened)

`agent.claude_agent_sdk.permission_mode` is passed straight to the CLI. What it
means:

| mode | what the CLI does with a tool call | Hermes guardian |
|---|---|---|
| `default` | prompts for anything not pre-approved; the prompt falls through to Hermes' `can_use_tool` callback | runs `tools/approval_smart.py` for EVERY Bash call that reaches the callback; on this lane that is an aux one-shot **CLI spawn per call** (`tools/approval_sdk_gateway.py`) |
| `acceptEdits` | auto-allows file edits, still prompts for commands | same as default for commands |
| `plan` | read-only planning, no writes | n/a |
| `dontAsk` | denies anything that would prompt | never runs |
| `bypassPermissions` | allows everything | never runs |
| `auto` | the CLI's own classifier screens each call; only unclear calls prompt | runs only for those prompts |

The fork default is `auto` (`hermes_cli/config_defaults.py`; upstream ships
`default`). Symptom of `default`: one Hermes turn with ~35 Bash calls spawned ~35
extra `claude` processes in a morning, CPU pinned, "Backend timeout" banners, and
plan usage burned on approval questions. `hermes doctor` prints the mode and its
cost (`hermes_cli/doctor_auth.py`); the transport logs a WARNING at startup when
the mode is `default`. Probe that proved it: `.sdkprobe/auto_mode_probe.py`
(auto: callback not invoked for a write command; default: invoked).

Set it per profile in `$HERMES_HOME/config.yaml`:

```yaml
agent:
  claude_agent_sdk:
    permission_mode: auto
```

## 2. The other config keys that matter on this lane

| key (`agent.claude_agent_sdk.*`) | fork value | why |
|---|---|---|
| `cli_path` | `~/.local/bin/claude` | follow `claude update`; the SDK's bundled CLI 400s on Fable 5.1. Aux one-shots use the same pin (`agent/claude_sdk_aux_client.py`). The pinned binary auto-updates (2.1.263 on 2026-09-07, 2.1.269 on 2026-09-11) |
| `plugins` | `[<conductor plugin dir>]` | `--plugin-dir` with `setting_sources: []` kept (isolation) |
| `session_name` | `hermes:{title}` | the CLI's `--name`; peers see the tab title in `ListAgents`. Renames go through `/rename` (instant when idle, deferred to the next turn when busy) |
| `deliver_background_results` | `true` | REQUIRED for peer `SendMessage` to land in a Hermes chat. Upstream default false drops it with a WARN while the sender sees success |
| `hybrid_mcp_bridge` | off | `hermes mcp add` servers reach the SDK session only with this on; otherwise the SDK sees hermes-tools + `plugins:` only |

Launch the desktop with `env -u ANTHROPIC_API_KEY`: a metered key in the env
blocks the subscription lane (fail-closed guard).

## 3. What the desktop shows, and the bugs behind "nothing is happening"

All four of these looked the same to the user: a timer ticking, no content.
They had four different causes. Check the specific one before restarting.

| date fixed | symptom | real cause | fix |
|---|---|---|---|
| 2026-09-09 | peer replies never appeared | desktop formatter painted `sdk_background_result` as a dead background process | `tui_gateway/session_notifications.py` persists + paints it as an assistant message |
| 2026-09-10 | streaming stopped after a while, turn "stuck" | websocket reconnect left the session on the detached-transport sentinel; the replay ring held 266/221 deltas nobody received | `write_json` fans detached frames out to live transports (`_fan_out_detached_event`) |
| 2026-09-10 | 404 storm in the main log | tile transcript reconcile ran without the row's profile | `use-background-sync.ts` reads under the row profile and latches gone tiles |
| 2026-09-11 | "not seeing any of the commands running for many minutes" | the SDK runtime only sent `tool_progress_callback("tool.started", name, …)`, and the gateway DROPS that event whenever a name is present (`tui_gateway/tool_progress.py` `_on_tool_progress`). A 9-minute turn ran 33 tool calls with zero `tool.start` in the ring | transport fires `on_tool_use` / `on_tool_result` per ToolUseBlock/ToolResultBlock; runtime routes them to `tool_start_callback` / `tool_complete_callback` (same pair the codex bridge uses) |
| 2026-09-11 | Settings pages (Voice, Safety, …) stuck on skeleton bars | not the backend (every config call answered in <40 ms): after a profile switch the page wipes its draft and waits for a NEW config object, but react-query keeps the old reference when the refetch is equal, so the seed effect never re-ran | `config-settings.tsx` seeds on `dataUpdatedAt` and refetches on switch. Recipe: `.sdkprobe/page_eval.py` dumped the react-query cache (success, idle, data present) and the component's hook state (draft null) over CDP |

Restart rule: Python backend changes need a desktop restart (the backend is a
child of Electron). Kill by PID only. Never `pkill -f apps/desktop` (it killed
the user's whole desktop once).

## 4. Triage recipes (copy, paste, read)

Find the backend for a profile, its port and its token:

```bash
pgrep -fl "hermes_cli.main --profile thinkbot serve"
lsof -nP -a -p <PID> -iTCP -sTCP:LISTEN | awk 'NR>1{print $9}'
ps eww -p <PID> | tr ' ' '\n' | grep HERMES_DASHBOARD_SESSION_TOKEN
```

Which turns are alive and which CLI belongs to which backend:

```bash
ps -eo pid,ppid,etime,pcpu,command | grep -E "[c]laude --output-format|hermes_tools_mcp_server|slash_worker"
tail -30 .hermes-test/profiles/thinkbot/logs/gui.log     # "tui prompt accepted" / "tui turn finished"
tail -20 .hermes-test/profiles/thinkbot/logs/errors.log
```

`tui_gateway.slash_worker` is a persistent per-session helper. Twelve minutes
old at 0% CPU is normal, not a stuck command.

Did the backend EMIT events for a session (replay ring, needs port + token from above):

```bash
.venv/bin/python .sdkprobe/session_list.py <PORT> <TOKEN>
.venv/bin/python .sdkprobe/events_since.py <PORT> <TOKEN> <ui_session_id...>
```

Healthy tool-heavy turn: `tool.start` and `tool.complete` counts in the kinds
dict. Only `message.delta` means the source is not emitting (this was the
2026-09-11 bug). `truncated=True` with hundreds of deltas and no client means
the transport was detached (the 2026-09-10 bug).

Did the RENDERER receive them (desktop must be started with CDP on 9223):

```bash
.venv/bin/python .sdkprobe/watch_events.py 25      # counts ws frames per session and type
node apps/desktop/scripts/live-drive.mjs eval "document.title"
```

What the CLI itself is doing (its own transcript, updated per message):

```bash
ls -t ~/.claude/projects/-Users-justin-Documents-Projects-Business-hermes-cntrl/*.jsonl | head -3
tail -2 <newest>.jsonl | cut -c1-400
```

A CLI at 0% CPU with no sockets is idle between API calls, not hung; the
transcript mtime tells you if it moved in the last minute.

Prove a transport change without the desktop (real CLI, real subscription):

```bash
env -u ANTHROPIC_API_KEY .venv/bin/python .sdkprobe/tool_card_probe.py   # expects PASS
env -u ANTHROPIC_API_KEY .venv/bin/python .sdkprobe/stream_probe.py
```

## 5. Cross-session messaging rules (peers are other Claude Code sessions)

- Peers address a Hermes tab by `hermes:<title>` (`ListAgents` shows it).
- A test handoff once said "merge main into the fork branch" and the receiving
  session DID it (checked out `main`, stripped the venv). Never route
  destructive verbs between sessions; prefix drills with `DRILL`. The rules
  live in `cntrl-plugins/cntrl_router/` (skill + README).
- Replies come back through `deliver_background_results`; they appear as a
  completed assistant message in the tab that owns the CLI session.

## 6. Things still open

- The renderer logs ~40 `useClientLookup: Clamped stale index` warnings per second
  (assistant-ui 0.14.24, `@assistant-ui/store`) while a tool-heavy turn streams.
  Harmless per message, but it means the message list re-renders constantly;
  candidate for the CPU-spike work.
- Aux one-shot client spawns a CLI per call; a persistent aux client would
  remove that cost entirely.
- The desktop has no group filter yet (`cntrl_groups` plugin is CLI/slash only).
- "Session controls unavailable" banner on a brand-new tab until the first turn.
- Upstream candidates: 404 retry storm, never-ended session prune, title
  generator scaffolding guard, SDK tool cards (this file, §3 last row).
