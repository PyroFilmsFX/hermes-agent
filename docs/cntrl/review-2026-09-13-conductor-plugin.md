# Conductor plugin review — 2026-09-13 (Phase 2C)

Seat: GPT-6 Astra @ high via Codex CLI, read-only. Job `w_20260913T173900Z_db37`,
full log `.claude/state/worker-spawn/logs/w_20260913T173900Z_db37.log` (439s).
Target: `/Users/justin/Documents/Projects/Business/thinkbot-plugin/plugins/conductor`
(dev checkout self-identifies as **3.57.5**; installed release is **3.60.2** at
`~/.claude/plugins/cache/thinkbot-plugin/conductor/3.60.2` — the seat audited both and
tags installed-only findings). Not yet triaged by a human.

## High (certain)
1. `.mcp.json:5` (dev + installed) — launcher blocks host-project discovery but still
   inherits `PYTHONPATH`/`PYTHONHOME`; the import-poisoning failure class survives.
   Fix: scrub `PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX UV_PROJECT` before the
   interpreter starts; isolated env. (Hermes side already scrubs — c0deb07abd — but the
   plugin must not depend on the host doing it.)
2. `scripts/tb_marker.py:1097`, `tb_seat_mcp.py:8502,8572` — concurrent wave closes can
   double-increment `waves_done` (stale-read `save()`). Fix: compare run/revision/expected
   wave under the mutation lock; reject duplicates.
3. `tb_seat_mcp.py:8925,8959`, `tb_row_ledger.py:2475` — completion/disposition writers load
   outside the lock and replace the whole ledger; concurrent ops erase acknowledged decisions.
   Fix: route through `update_ledger()`.
4. `tb_seat_mcp.py:2219,5840` — live-job guard keys on run/lane/cwd only → rejects a second
   distinct council/validator seat on the same run; stale active records not refreshed first.
5. `tb_seat_mcp.py:6299,6305` — post-fork `record_wave_spawn()` failure returns before the
   monitor starts → unsupervised running child.
6. `tb_seat_mcp.py:4919,4423` — monitor never enforces `timeout_sec` itself; a disconnected
   SDK client leaks indefinitely-running workers. **Directly hits Hermes SDK-lane sessions.**
7. `tb_seat_mcp.py:8567`, `tb_marker.py:3590,4897` — MCP wave-close omits
   `wave_receipt.tree_sha`/`wave_base_tree` advancement → later waves diff a stale baseline.
8. `tb_seat_mcp.py:8483`, `tb_marker.py:4698,4717` — MCP wave-close skips the CLI's
   diagnostic-enforcement and expired-lease checks. Fix: one shared admission function.
9. `tb_seat_mcp.py:8061,8070,1700` — `record_outcome()` mutates the sealed `role` via
   `update_job()`, reports success, then breaks the seal on later reads.
10. `policy.json:39`, `tb_workers.py:1178` (installed :1186) — Composer still a fallback and
    restorable via `TB_IMPL_DEFAULT=composer` despite retirement (owner ruling 2026-09-10).
11. installed `tb_seat_mcp.py:8714`, `policy.json:10` — `council_seat("codex")` hardcodes
    `gpt-5.6-sol` @ xhigh while policy says Astra. Fix: resolve model+effort from policy.

## Medium (certain) — grouped
- **Version drift (12):** dev tree 3.57.5 vs installed 3.60.2; reconcile before acting.
- **Env/routing logic (13,14,15,17,19,20,22):** unset `CLAUDE_PROJECT_DIR` → `Path("")`
  takes the daemon-cwd branch (`:5359`); routing refresh ignores probe exit code (`:5403`);
  **a session id alone classifies a non-TTY SDK session as interactive and rejects headless
  dispatch (`:5576,5655`) — Hermes SDK lane**; omitted effort stages `medium` over Codex's
  documented `high` (`:5864,6081`); job persistence vs snapshot projection use separate locks
  (`:2898,2902,2808`); multi-wait rescans + rewrites snapshots on every poll (`:7412,7813`);
  Sonnet floor filtered out of MCP fallback (`VALID_WORKERS`, `:302,5480`).
- **Relay agents can't do what their prompt says (16):** codex-worker told to run CLI
  fallbacks but allowlist has no Bash/Write → return a structured host handoff instead.
- **Astra effort (18):** installed `codex-council.md:63,67`, `tb_render_doctrine.py:193,197`
  default/fallback to xhigh; owner default is high.
- **Doctrine contradictions (21):** model-tiers mandate Sonnet-only validation + forbid Sonnet
  implementation, vs strong-pair validation + native Sonnet floor in policy.
- **Composer retirement sweep incomplete (23–28):** conductor.config.json, tb_decompose.py:146
  (survives in 3.60.2), tb_workers.py:1093, tb_render_doctrine.py, hooks/tb_inject.py:19-20,
  tb_refresh_policy.txt, tb_wave_ledger.py:46, agents/*.md (composer-worker, fable-worker,
  codex-worker, glm-council, fireworks-council, devin-worker), skills (conductor, tb-plan,
  tb-build, tb-sa incl. dispatch-schema.json, tb-setup, setup-env, tb-loop, tb-run steps
  0/2/3/5, preflight, _router), references (transport-contract, model-tiers, execution,
  continuous-execution, investigation, codex-flags, portable-mode), docs/dev-standards.

## Seat summary (verbatim)
- Overall health: substantial safeguards, but startup isolation, concurrent transitions, and
  routing consistency remain unreliable.
- First fix: scrub launcher environment and isolate the interpreter.
- Second fix: make marker transitions and ledger changes transactional, with shared CLI/MCP admission.
- Third fix: unify retirement, model, and effort selection across dispatch, overrides, and
  generated prompts.
- Unverified: runtime reproductions, actual SDK environment, provider behavior. No Devin
  auto-route found (devin-worker.md exists but isn't a live dispatch target).

## Next step
Triage with Justin → open items in the thinkbot-plugin repo (not here). Items 1, 6, 15 are the
ones that bite Hermes SDK-lane sessions today.
