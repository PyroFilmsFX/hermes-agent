---
name: fork-inventory
description: Refresh the cntrl fork inventory after an upstream sync or any core-file commit. Triggers - /fork-inventory, update the fork inventory, what do we carry, sync upstream, merged upstream, after rebase.
---

# fork-inventory

Keeps `docs/cntrl/fork-inventory.md` (the why) and the generated snapshot
(the what, with dates) honest over time.

## Steps

1. Refresh refs:
   ```bash
   git fetch origin
   git fetch origin pull/65982/head:pr-65982-sep7
   ```
2. Run the generator (from repo root, project venv):
   ```bash
   .venv/bin/python scripts/cntrl/fork_inventory.py --check
   ```
   It writes `docs/cntrl/fork-inventory.generated.md`, appends one row to
   `docs/cntrl/fork-inventory-log.md`, and exits 1 if a carried commit
   touches a core file that §3 of `fork-inventory.md` does not list.
3. If it exits 1: add a row to §3 of `docs/cntrl/fork-inventory.md` for each
   unlisted file (file · what · why). Re-run until it exits 0.
4. Update the "Last audit" date at the top of `fork-inventory.md`, and if a
   carried commit was dropped because upstream now has it, delete its §3 row
   and say so in §1.
5. Commit all three files together with prefix `cntrl:`.

## Rules

- Never hand-edit the generated file or the log.
- A core edit without a §3 row is a bug, not a style issue.
- Run this before `/tb-ship` on any branch that touches `agent/`,
  `hermes_cli/`, `gateway/`, `tools/`, `run_agent.py`, `pyproject.toml`.
