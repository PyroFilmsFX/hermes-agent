# Conductor build status record (tb-build-status/v1)

The `tb-build-status/v1` record is a per-build status document written by `conductor-worker`
and read by Hermes. It powers the global Conductors page and pane in Hermes Desktop,
the per-session build strip, and the `GET /api/profiles/conductors` backend route.

The record is a **derived view, never an input** to conductor gates. It is rebuilt whole from
conductor's internal sources (active build marker, row ledger, predictions, job store, lane
receipts, and daemon refusal counters) under a sidecar lock, using atomic write with mode 0600.
Conductor writes the file; Hermes only reads it. If writing or refreshing the status file fails,
the triggering conductor verb never fails.

## File location and lifecycle

The status record is placed as a sibling to the active conductor build marker in the state directory:

- **Namespaced builds:** `<state>/builds/<build_id>/tb-build-status.json`
- **Flat / legacy builds:** `<state>/tb-build-status.json`

Hermes derives the status path directly from the marker path it already trusts. The file is read
using hardened file descriptors with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` and re-checked via
`fstat`. Any symlinked file, non-regular file, or file exceeding 64 KiB (65,536 bytes) is
refused with reason `unreadable`.

### Writer triggers and completion

Conductor refreshes the status file:
1. After every marker write (`tb_atomic` post-write hook);
2. After ledger transitions: `wave-done`, `wait` / `unwait`, and `block` / `unblock`;
3. After prediction updates: `estimate reforecast`;
4. After every daemon worker spawn result or spawn refusal.

There is no background polling timer for writes: when no conductor events occur, nothing is written.
On build completion, conductor's final refresh writes `phase: "done"`. Conductor then archives or
unlinks the file alongside the marker. Hermes displays a `done` record for at most 10 minutes (matching
the relay job terminal retention window) before dropping the row.

## Record schema

All timestamps are UTC ISO-8601 strings ending in `Z`. All text fields contain plain text only
(no markdown, no ANSI escapes, and no ASCII or Unicode control characters). Every field is optional;
Hermes falls back gracefully if any field is omitted or malformed.

```json
{
  "schema": "tb-build-status/v1",
  "seq": 42,
  "written_at": "2026-09-29T07:12:03Z",
  "conductor_version": "3.62.0",
  "build": {
    "run_id": "cc546d7b99dc45d6a829da8e2c77a673",
    "session_id": "6d739ad6-1234-5678-9abc-def012345678",
    "build_id": "b_123",
    "hermes_session_id": null,
    "binding_nonce": null,
    "plan_title": "Hermes worker plan b9/b10",
    "branch": "cntrl-hermes-worker",
    "armed_at": "2026-09-29T06:39:40Z"
  },
  "phase": "build",
  "progress": {
    "waves": { "done": 1, "total": 4 },
    "units": { "done": 7, "running": 3, "failed": 0, "remaining": 12, "total": 22 },
    "current_wave": { "index": 2, "id": "W2", "title": "b10 binding" },
    "current_units": [
      { "id": "H5", "title": "attest.py verifier", "seat": "agy", "since": "2026-09-29T07:00:00Z" }
    ],
    "remaining_waves": [
      { "index": 3, "id": "W3", "title": "#49 Conductors page", "units": 19 }
    ]
  },
  "estimate": {
    "unit": "work_hours",
    "p50": 6.5,
    "p90": 10.0,
    "basis": "reforecast",
    "as_of": "2026-09-29T07:10:00Z",
    "prediction_id": "pred-42"
  },
  "gates": [
    {
      "id": "g-7209",
      "kind": "ci",
      "label": "CI run 36533732367 on sync/land-b9",
      "since": "2026-09-29T07:05:00Z",
      "due": null,
      "ref": "36533732367",
      "state": "waiting",
      "waiter": true
    }
  ],
  "seats": {
    "agy": { "spawned": 12, "running": 2, "succeeded": 8, "failed": 1, "refused": 3 },
    "muse": { "spawned": 0, "running": 0, "succeeded": 0, "failed": 0, "refused": 0 },
    "codex": { "spawned": 4, "running": 1, "succeeded": 3, "failed": 0, "refused": 0 },
    "sonnet": { "spawned": 6, "running": 0, "succeeded": 6, "failed": 0, "refused": 1 },
    "opus": { "spawned": 2, "running": 0, "succeeded": 2, "failed": 0, "refused": 0 },
    "other": { "spawned": 1, "running": 0, "succeeded": 1, "failed": 0, "refused": 0 }
  },
  "other_names": ["grok"],
  "refusals": [
    {
      "seat": "agy",
      "code": "stale_daemon",
      "count": 3,
      "last_at": "2026-09-29T07:11:00Z",
      "note": "tb-workers daemon predates 3.61.10"
    }
  ],
  "lanes": {
    "running": 3,
    "stale": 0,
    "cap": 6,
    "job_ids": ["w_20260929T065856Z_06fd"]
  },
  "ci": [
    {
      "kind": "run",
      "ref": "36533732367",
      "branch": "sync/land-b9",
      "pr": null,
      "state": "pending",
      "url": "https://github.com/example/repo/actions/runs/36533732367",
      "checked_at": "2026-09-29T07:11:30Z"
    }
  ],
  "owner_blockers": [
    {
      "id": "ob-1",
      "action": "approve",
      "label": "Reinstall owner verifier (admin prompt)",
      "since": "2026-09-29T07:00:00Z",
      "ref": null
    }
  ],
  "last_activity": {
    "at": "2026-09-29T07:12:00Z",
    "what": "wave-done W1",
    "verb": "wave-done"
  }
}
```

## Field reference

### Top-level fields

| Field | Type | Limits / Enum | Description |
| --- | --- | --- | --- |
| `schema` | string | Exact `"tb-build-status/v1"` | Required format identifier. Unknown formats yield `unknown_schema`. |
| `seq` | integer | $\ge 0$, monotonic | Write counter for this build. Lower values are rejected as rollbacks. |
| `written_at` | string | ISO-8601 UTC string ending in `Z` | Freshness timestamp. Evaluated against file mtime and `now`. |
| `conductor_version` | string | Cap 64 chars | Version of conductor writing the record. |
| `build` | object | Required join keys | Join keys tying this status record to the active marker. |
| `phase` | string | `plan`, `build`, `review`, `land`, `waiting`, `blocked`, `closing`, `done` | Current build phase. Unrecognized strings map to `"other"`. |
| `progress` | object | Waves, units, and flight arrays | Structured progress counts and wave titles. |
| `estimate` | object | Work hours only | Remaining estimated work time (`p50`, `p90`). Absent if unestimated. |
| `gates` | array | Cap $\le 16$ items | Active gates and waits. Excluded from work time estimate. |
| `seats` | object | Standard seat keys | Spawn, outcome, and refusal counts by seat. |
| `other_names` | array | Cap $\le 8$ items, string cap 64 | Labels for seats grouped under `other` (e.g. `grok`). |
| `refusals` | array | Cap $\le 10$ items | Recent worker spawn refusals aggregated by `(seat, code)`. |
| `lanes` | object | Running, stale, cap, job IDs | Running worker lane counts and job identifiers. |
| `ci` | array | Cap $\le 6$ items | Conductor's most recent direct CI/PR observations. |
| `owner_blockers` | array | Cap $\le 8$ items | Actions required by the human owner to unblock progress. |
| `last_activity` | object | Timestamp, verb, and summary | Most recent event details for liveness derivation. |

### `build` join keys and metadata

| Field | Type | Limits | Description |
| --- | --- | --- | --- |
| `run_id` | string | Matches marker `build_run_id` or `run_id` | Unique build run identifier. Required for join. |
| `session_id` | string | Matches marker `session_id` | Claude CLI session identifier. Required for join. |
| `build_id` | string \| null | Cap 64 chars | Namespace identifier (e.g. `b_123`), or `null` for legacy flat builds. |
| `hermes_session_id` | string \| null | Cap 64 chars | Hermes session ID bound via attestation (b10+). |
| `binding_nonce` | string \| null | Cap 64 chars | Verified attestation nonce from b10 binding. |
| `plan_title` | string \| null | Free text, cap 120 chars | Human-readable title of the plan. Absolute paths are dropped. |
| `branch` | string \| null | Cap 120 chars | Git branch name at write time. Absolute paths are dropped. |
| `armed_at` | string \| null | ISO-8601 UTC string (cap 64) | Time the build was initially armed. |

### `progress` fields

| Field | Type | Limits | Description |
| --- | --- | --- | --- |
| `waves.done` | integer | $\ge 0$ | Number of completed waves. |
| `waves.total` | integer | $\ge 1$ | Total planned waves. |
| `units.done` | integer | $\ge 0$ | Number of completed units. |
| `units.running` | integer | $\ge 0$ | Number of currently running units. |
| `units.failed` | integer | $\ge 0$ | Number of failed units. |
| `units.remaining` | integer | $\ge 0$ | Number of remaining planned units. |
| `units.total` | integer | $\ge 0$ | Total units across all waves. |
| `current_wave.index` | integer | $\ge 1$ | 1-based index of current wave. |
| `current_wave.id` | string | Cap 32 chars | Identifier for current wave (e.g. `"W2"`). |
| `current_wave.title` | string | Free text, cap 80 chars | Descriptive title for current wave. |
| `current_units[]` | array | Cap $\le 8$ items | Units currently in flight (`id` cap 32, `title` cap 80, `seat` cap 32, `since` cap 64). |
| `remaining_waves[]` | array | Cap $\le 12$ items | Subsequent waves (`index` $\ge 1$, `id` cap 32, `title` cap 80, `units` $\ge 0$). |

### `estimate` fields

Estimate represents remaining pure work time only; it explicitly excludes gate wait times.

| Field | Type | Limits | Description |
| --- | --- | --- | --- |
| `unit` | string | Exact `"work_hours"` | Unit of measurement. |
| `p50` | float | Finite, $\ge 0.0$, $\le p90$ | 50th percentile estimated remaining hours. |
| `p90` | float | Finite, $\ge 0.0$, $\ge p50$ | 90th percentile estimated remaining hours. |
| `basis` | string | `predict`, `reforecast`, `manual` | Forecasting basis. Unrecognized strings map to `"other"`. |
| `as_of` | string | ISO-8601 UTC string (cap 64) | Timestamp when estimate was produced. |
| `prediction_id` | string \| null | Cap 64 chars | Identifier of prediction ledger entry. |

If `p50 > p90`, if either value is negative or non-finite, or if types are invalid, the entire `estimate` block is blanked to `null`.

### `gates[]` fields

Lists external dependencies and dated gates currently blocking progression. Cap $\le 16$ items.

| Field | Type | Limits | Description |
| --- | --- | --- | --- |
| `id` | string | Cap 64 chars | Unique identifier for gate. |
| `kind` | string | `ci`, `owner`, `relaunch`, `review`, `external`, `date` | Gate category. Unknown values map to `"other"`. |
| `label` | string | Free text, cap 120 chars | Descriptive label. Secrets masked; paths stripped. |
| `since` | string | ISO-8601 UTC string (cap 64) | When the wait started. |
| `due` | string \| null | ISO-8601 UTC string (cap 64) | Due time for dated gates. |
| `ref` | string \| null | Cap 64 chars | Run ID, PR number, or decision ID. |
| `state` | string | `waiting`, `passed`, `failed`, `cancelled` | Gate status. Unknown values map to `"other"`. |
| `waiter` | boolean \| null | Boolean | For CI gates: indicates if a background poller is armed. |

### `seats` and `refusals` fields

Seat counts track worker lane allocation for this specific build. Standard seat keys are `agy`,
`muse`, `codex`, `sonnet`, `opus`, and `other`. Gemini lanes count under `agy`. Native Claude
lanes appear under `sonnet` and `opus`.

Each seat object contains integer counts ($\ge 0$): `spawned`, `running`, `succeeded`, `failed`, and `refused`.

> **Refusals as a lower bound (`refused_min`):**
> Worker spawn refusals can only be attributed to a build once the spawn request resolves a valid
> state directory or marker path. Pre-resolution daemon rejections are unattributed across builds.
> Conductor-worker records refusals it can attribute, and Hermes exposes `refused` as `refused_min`.
> The UI presents refused counts as a lower bound rather than an exact total.

`refusals[]` captures up to 10 recent refusal events:
- `seat`: string (cap 32 chars)
- `code`: string (cap 64 chars), e.g. `stale_daemon`, `contract_gaps`, `cap_exceeded`
- `count`: integer $\ge 0$
- `last_at`: ISO-8601 UTC timestamp (cap 64 chars)
- `note`: free text (cap 120 chars). Conductor strips path-like substrings prior to capping; Hermes enforces this sanitization again on read.

### `lanes`, `ci`, `owner_blockers`, and `last_activity`

- **`lanes`**: `running` (int $\ge 0$), `stale` (int $\ge 0$), `cap` (int $\ge 0$ or null), `job_ids` (array of up to 12 strings, cap 64).
- **`ci[]`**: up to 6 items; `kind` (`run` or `pr`), `ref` (cap 64), `branch` (cap 120, not a path), `pr` (int, str, or null), `state` (`pending`, `success`, `failure`, `cancelled`, `unknown`), `url` (cap 500, must start with `https://github.com/`), `checked_at` (cap 64). Fresh CI observations ($\le 10$ minutes) take precedence over GitHub API queries.
- **`owner_blockers[]`**: up to 8 items; `id` (cap 64), `action` (`answer`, `approve`, `decide`, `relaunch`, `do`), `label` (free text cap 120), `since` (cap 64), `ref` (cap 64).
- **`last_activity`**: `at` (cap 64), `what` (free text cap 80), `verb` (cap 64). Feeds the unified liveness engine.

---

## Join rules and validation

Hermes validates the record against the active marker before accepting it:

1. **Schema match:** `schema` must be exactly `"tb-build-status/v1"`.
2. **Run ID join:** `build.run_id` must match `marker.build_run_id` (or fallback `marker.run_id`).
3. **Session ID join:** `build.session_id` must match `marker.session_id`.

If any join key does not match, Hermes marks the status read as `mismatched` (`valid = false`).
Hermes ignores the record entirely and displays derived marker fallbacks.

### Monotonic sequence high-water mark

Hermes tracks the highest `seq` seen per `(marker_path, run_id)` key. If a status read presents
a lower `seq` than previously observed for that build, it is rejected as an out-of-order rollback.
Hermes preserves the previous valid record while updating its freshness clock. A new `run_id`
resets the sequence counter to 0.

### Freshness and clock clamp

Hermes calculates effective status time as:
$$\text{status\_at} = \min(\text{written\_at}, \text{mtime} + 5\text{ minutes})$$

To prevent clock skew or future-dated records from appearing perpetually fresh, `status_at` is
further clamped to current time (`now`).

- **Fresh:** $\text{now} - \text{status\_at} \le 30\text{ minutes}$ (1,800 seconds).
- **Stale:** $\text{now} - \text{status\_at} > 30\text{ minutes}$.

**Staleness is display-only.** A stale record causes cell values to display "as of HH:MM" and
dims the estimate tooltip. An aging status record **never** hides a row and **never** marks the
build itself idle or stale. Row liveness is determined solely by owner session transport state,
active turn activity, and running lane heartbeats.

---

## Field coercion and fallbacks

Hermes validates each field independently. A wrong type or constraint violation blanks only that
specific field without invalidating the record as a whole.

When the status record is missing, mismatched, or has blanked fields, Hermes falls back to marker
and job store data:

| Column | From status record | Marker / job fallback | UI badge |
| --- | --- | --- | --- |
| Wave | `progress.waves`, `current_wave` | `marker.waves_done`, `waves_total`, `min(done+1, total)` | none |
| Unit (current) | `current_units[0]` (+N) | `marker.wave_stage` | `≈` |
| Remaining | `units.remaining`, `remaining_waves` | `waves_total - waves_done` waves; units "—" | `≈` |
| Estimate 50/90 | `estimate` | "—" ("Conductor has not estimated this build") | none |
| Gates | `gates[]` | `marker.waits[]` (`kind`, `ci_ref`, `what` $\to$ `label`, `at` $\to$ `since`) | `≈` |
| Seats & refusals | `seats`, `refusals` | Relay jobs in state dir by `served_seat` (relay only; Sonnet/Opus "?") | `≈ relay only` |
| Lanes | `lanes` | Running/stale relay jobs across state dir | `≈ workspace` |
| CI / PR | `ci[]` ($\le 10$ min old) | GitHub GraphQL read through shared PR cache | none |
| Owner blockers | `owner_blockers[]` | `marker.blocked == true` $\to$ `waiting_on` / `blocked_reason` | `≈` |
| Last activity | `last_activity` | $\max(\text{marker mtime}, \text{newest lane heartbeat}, \text{status written\_at})$ | none |

The `≈` badge indicates that the value was derived by Hermes rather than reported by conductor.

---

## Privacy and security hygiene

Status records are treated as untrusted data across security boundaries. Hermes enforces strict hygiene:

1. **File size boundary:** The file must not exceed 64 KiB (65,536 bytes). Oversized files are rejected with `reason: "unreadable"`.
2. **Symlink traversal refused:** Every path component is opened with `O_NOFOLLOW`. Symlinks are rejected with `reason: "unreadable"`.
3. **Secret hygiene:** All free text passes through `mask_stored_text`. Anthropic keys, OpenAI tokens, Bearer JWTs, SSH keys, and hex credentials are redacted.
4. **No absolute paths:** Absolute filesystem paths are prohibited:
   - Any free-text field (`plan_title`, `branch`, refusal `note`) starting with `/`, `~`, `\\\\`, or a Windows drive letter (`C:\`) is dropped to `null`.
   - Embedded absolute paths matching system directories (`/private/tmp`, `/Users/...`, `/var/...`) inside labels are stripped.
   - `branch` must be a git ref name, not a file path.
   - `ci[].url` must begin with `https://github.com/`.
5. **Control character stripping:** ANSI escape sequences (`\x1b[...]`) and ASCII/Unicode control characters are stripped.

---

## Schema versioning

- **Additive changes stay in v1:** New optional fields, additional enum values, and new seat names remain in `tb-build-status/v1`. Unrecognized enum values fall back to `"other"`, and extra fields are ignored.
- **Breaking changes require v2:** Renaming, retyping, or re-meaning fields requires `tb-build-status/v2`.
  - Conductor dual-writes `tb-build-status.json` (v1) and `tb-build-status.v2.json` across minor version migrations.
  - A Hermes version that encounters only a v2 record marks it `unknown_schema`, uses marker fallbacks, and displays the prompt: *"This conductor reports a newer status format. Update Hermes."*

---

## Reference fixtures

Hermes and conductor-worker share a golden fixture suite located in `tests/fixtures/conductor/`:

- `status_v1_valid.json`: Valid complete record, parses with all fields intact.
- `status_v1_mismatched.json`: Differing `run_id` and `session_id`, produces `reason: "mismatched"`.
- `status_v1_v2_only.json`: Future schema `tb-build-status/v2`, produces `reason: "unknown_schema"`.
- `status_v1_oversize.json`: File > 64 KiB, produces `reason: "unreadable"`.
- `status_v1_hostile.json`: Malformed types, embedded API tokens, ANSI sequences, and absolute paths, verifying sanitization and field blanking.

Fixture conformance is verified by `tests/tui_gateway/test_conductor_status_fixtures.py`.
