# Owner grants

Owner grants are signed, session-bound evidence for a specific owner decision. They let a
conductor hook verify a decision without carrying signing keys or implementing cryptography.
The quote records evidence; only the signed scopes grant authority.

Sessions can hand the owner a decision to deliver with a [`:::send-to` block](./send-to-directive.md);
the owner's click and confirm produce the grant.

## Grant format

A grant file is named `<issued_at_ms>-<grant_id>.json` in the anchor-pinned grants directory.
Its JSON envelope has exactly these fields:

```json
{"format":"hermes-owner-grant/v1","kid":"ok_…","payload":"<base64url bytes>","sig":"<base64url signature>"}
```

`payload` is the canonical UTF-8 JSON byte sequence, encoded as unpadded base64url. The
Ed25519 signature covers `hermes-owner-grant/v1`, a zero byte, and those exact payload
bytes. `grant_id` is `og_` plus a hash-derived identifier for the payload bytes. Do not
re-serialize the payload before signature verification. The payload binds the owner uid,
decision and source session, target session(s), backend, scopes, issue/delivery/expiry times,
gesture, nonce, subject when required, and the approved text or its SHA-256.

The verifier checks the envelope and key, signature, audience, owner uid, session binding,
time bounds, every requested scope, required subject, approved text match, revocation and
single-use state. A conductor-scope grant must bind its target to a Claude CLI session id;
otherwise verification denies it with `claude_session_unbound`. Quote-only grants may omit
that binding. A transient-API retry starts a fresh Claude session, so a conductor grant bound
to the previous session fails closed (re-send the decision). Grant files and the revocation/consume ledgers are under the grants
directory pinned in the trusted anchor, not under `HERMES_HOME`.

## Scope grammar and policy

Scopes have the exact form `conductor:<class>:<name>`. Class is one of `allowlist`, `gate`,
`marker`, `prod`, `answer`, `defer`, `override`, `gc`, `policy`, `spend`, or `continuity`; name starts with a lowercase letter or digit and continues with up to
62 lowercase letters, digits, or hyphens. Matching is exact. There are no wildcards. A
grammar-valid scope may be used before it has a catalog label; new dialog labels belong in
`hermes_owner_grant/scopes.json`.

| Class | Default TTL | Maximum TTL | Use and confirmation |
| --- | ---: | ---: | --- |
| quote-only (no scope) | 7 days | 7 days | Reusable evidence only; grants no conductor authority |
| allowlist | 12 hours | 72 hours | Reusable; no subject required |
| gate | 12 hours | 72 hours | Reusable; no subject required |
| marker | 1 hour | 4 hours | Single-use |
| prod | 15 minutes | 1 hour | Single-use, subject required, Touch ID when available |
| answer | 1 hour | 4 hours | Single-use, subject required |
| defer | 1 hour | 4 hours | Single-use, subject required |
| override | 1 hour | 4 hours | Single-use, subject required |
| gc | 1 hour | 1 hour | Single-use, subject required |
| policy | 15 minutes | 1 hour | Single-use, subject required, Touch ID when available |
| spend | 15 minutes | 1 hour | Single-use, subject required, Touch ID when available |
| continuity | 15 minutes | 15 minutes | Single-use, subject required |

TTL values are maxima, not promises that a grant will remain valid. A verifier request may
ask for several scopes; the strictest class limit applies. Prod hooks should bind the subject
to the action digest. Marker and prod checks use `--consume`, and so do checks for every scope
in the `answer`, `defer`, `override`, `gc`, `policy`, `spend` and `continuity` classes. The
scope loader refuses a catalog in which any of those seven classes is reusable or
subject-free.

### Subject grammar

The hook passes the subject with `--subject`, and the verifier compares it with the signed
`payload.subject[scope]` by exact string equality. Missing is `subject_required`; any
difference is `subject_mismatch`. Each catalogued scope below also fixes the grammar of its
signed subject: a grant whose signed subject falls outside that grammar is `malformed` and
authorizes nothing, even for a request that repeats the same string.

In the table, `<id>` is an ASCII letter or digit followed by up to 127 letters, digits, `.`,
`_` or `-` (never a colon), and `<sha256>` is 64 lowercase hex characters.

| Scope | Label | Subject |
| --- | --- | --- |
| `conductor:answer:stage-variant` | Answer a conductor stage question | `<question_sha256>:<option_index>`: a `<sha256>` and a 1-based option number with no leading zeros |
| `conductor:defer:wave-or-unit` | Defer a planned wave or unit | `<build_id>:<wave_or_unit_id>`, two `<id>`s |
| `conductor:policy:standing-approval` | Enable a standing-approval rule | `<rule_id>:<rule_sha256>`, an `<id>` and a `<sha256>` |
| `conductor:policy:unsandboxed-write` | Allow unsandboxed writes for a CLI | `<cli>`: a lowercase letter or digit, then up to 63 lowercase letters, digits, `.`, `_` or `-` |
| `conductor:override:review-budget` | Override the review budget | `<build_id>:<wave>:<fan\|recheck>`, two `<id>`s and the literal `fan` or `recheck` |
| `conductor:gc:prune-lanes` | Prune reviewed lanes | `<dry_run_manifest_sha256>`, a `<sha256>` |
| `conductor:spend:fly` | Start paid Fly lanes | `<fly_app>:<usd_cap>`: a Fly app name (lowercase letter or digit, then up to 62 lowercase letters, digits or `-`) and a positive decimal with no leading zeros and at most two decimals, such as `25` or `0.5` |
| `conductor:continuity:session-relaunch` | Rebind a session's marker after a relaunch | `<old_claude_sid>:<new_claude_sid>`, two different `<id>`s |

The exact patterns live in `SUBJECT_GRAMMAR` in `hermes_owner_grant/scopes.py`. The shared
vectors in `tests/fixtures/owner_grant_subject_vectors.json` pin the Python verifier and the
desktop signer to the same patterns. Scopes of the existing classes (`prod` included) keep
their earlier rule: any non-empty subject, matched exactly.

Only the desktop main process issues `conductor:continuity:session-relaunch`, and only when it
relaunches a session's backend and the persisted Claude SDK session id for that Hermes session
is the old id. No IPC channel, gateway method or tool can request one.

### Stage-question answers

A conductor stage question is written as a directive in a session reply:

```text
:::stage-question{question_sha256="06101283618d4dbc962eca91fd29273749b86463db0dbcffe9127597861ce6fa" scope="conductor:answer:stage-variant"}
Which layout should stage 3 ship?
1. Keep the current layout
2. Ship the compact layout
3. Defer the choice to the next wave
:::
```

The body is every line between the opening line and the closing `:::` line: the question text
and then the numbered options. `question_sha256` is the SHA-256 of that body, computed as:

- replace every CRLF with LF (a lone CR stays);
- trim only U+0020 space, U+0009 tab and U+000A line feed from both ends;
- apply no Unicode normalisation;
- hash the UTF-8 bytes and write the digest as lowercase hex.

The desktop app shows the question with one button per option. It recomputes the hash and
disables the buttons when it differs from `question_sha256`. A click signs through the same
confirm-and-sign path as other owner forwards, and the owner still confirms in the system
dialog. The resulting grant carries `conductor:answer:stage-variant` with the subject
`<question_sha256>:<option_index>`, where the index is 1-based. For the example above, choosing
option 2 signs `06101283618d4dbc962eca91fd29273749b86463db0dbcffe9127597861ce6fa:2`.

The renderer marks each directive it parses with a nonce that is new on every load of the
app. Authors never write the nonce. A fence that imitates the renderer's internal form without
the current nonce shows as plain text and makes no button. The same rule applies to
`:::send-to` blocks. Surfaces without the desktop renderer show the directive as raw text.

## Session bindings

A session binding associates a Hermes session (`hermes_session_id`) with a verified project
workspace on disk (`project_root`, resolved to the git worktree toplevel, along with
`repo_common_root` and `repo_remote`), recording durable owner intent. It allows conductor to
resolve "my build" for a session and preserve build ownership across app relaunches, backend
restarts, and transient-API retries without requiring manual marker rebinds.

A binding is created **exclusively by an owner gesture**. The desktop renderer enforces an
app-chrome main-frame sender check and a confirmation rate gate (maximum 10 requests per minute,
with a 3-second cooldown after cancellation; see `apps/desktop/electron/session-binding-ipc.ts`
and `apps/desktop/electron/owner-forward-confirm.ts`). The owner clicks the binding pill in the
chat header titlebar (`apps/desktop/src/app/chat/binding-pill.tsx`) or selects "Bind to project…"
from the session row menu (`apps/desktop/src/app/chat/sidebar/session-actions-menu.tsx`).
Workspace suggestions derived from the session's workspace key (`git_repo_root` or `cwd` via
`hermes_state_sessions.py:workspace_key`) are displayed as visual hints ("Bind to `<repo>`?"),
but are **never signed without an explicit owner click**. Auto-binding sessions created from a
project's "New session" button (design decision O1) is a documented follow-up and is **not
shipped in this build**.

Re-binding or unbinding a session with an active live-attested build requires a native macOS
confirmation dialog (`confirmRebind` in `apps/desktop/electron/session-binding-ipc.ts`, decision
O2); the initial bind uses an in-app confirm dialog.

### Binding records and launch attestations

Session bindings use two distinct signed artifacts. Both reuse the owner's Ed25519 key pair
(`apps/desktop/electron/owner-grant-key.ts`) and root anchor (`hermes_owner_grant/anchor.py`),
avoiding duplicate Keychain items or separate anchor entries. Strict domain separation
(`signDomain`) prevents signatures from crossing: each artifact specifies its own wire format and
domain prefix (`format + "\0" + payload_bytes`), and neither format can verify as a decision
grant (`hermes-owner-grant/v1`), binding record, or launch attestation in the other's verifier
(`hermes_owner_grant/attest.py`, `apps/desktop/electron/owner-grant-crosslang.test.ts`). Neither
artifact is a scope class in `scopes.json`, and neither writes to the single-use consume ledger.

1. **Binding record (`hermes-session-binding/v1`)**:
   - **Format and domain**: format `hermes-session-binding/v1`, domain prefix
     `hermes-session-binding/v1\0` (`BINDING_DOMAIN_PREFIX` in `hermes_owner_grant/attest.py:42`).
   - **Audience**: `["hermes-main"]` (`BINDING_AUDIENCE`), never contains `hermes-owner-verify`.
   - **Storage path**: `<grants_dir>/session-bindings/<profile>/<hermes_session_id>.json`.
   - **Permissions**: Directory mode 0700, file mode 0600 written via atomic `wx` temp file and
     rename (`apps/desktop/electron/session-binding-store.ts:writeBindingFile`).
   - **Purpose and verification**: Represents durable owner intent. It has no expiry timestamp
     (`expires_at`), so conductor never evaluates it. Electron main manages this store
     (`apps/desktop/electron/session-binding-store.ts`). At load, main requires an **active
     `kid`** from the anchor; records signed with retired, unknown, or revoked keys load in state
     `needs_reconfirm` and are not verified. In-memory `seq` counters prevent rollback. Upon key
     rotation, main's `SessionBindingStore.resignAll()` re-signs all verified in-memory records
     with the new active key. The Python backend also verifies this record using
     `hermes_owner_grant.attest.verify_binding_file` before setting `TB_STATE_ROOT`
     (`agent/transports/claude_agent_sdk_session_config.py:246-255`).
   - **Payload schema**:
     ```json
     {
       "v": 1,
       "aud": ["hermes-main"],
       "owner_uid": 501,
       "profile": "default",
       "hermes_session_id": "hs_01JF...",
       "state": "bound",
       "seq": 1,
       "binding_nonce": "MFRGGZDFMZTWQ2LK",
       "bound_at": 1759140000000,
       "project_root": "/abs/path/to/worktree",
       "repo_common_root": "/abs/path/to/repo",
       "repo_remote": "git@github.com:example/repo.git",
       "project_id": null,
       "carried_from": null
     }
     ```

2. **Launch attestation (`hermes-launch-attestation/v1`)**:
   - **Format and domain**: format `hermes-launch-attestation/v1`, domain prefix
     `hermes-launch-attestation/v1\0` (`DOMAIN_PREFIX` in `hermes_owner_grant/attest.py:31`).
   - **Audience**: `["conductor:session-binding"]` (`AUDIENCE` in `hermes_owner_grant/attest.py:32`),
     never contains `hermes-owner-verify`.
   - **Storage path**: `<grants_dir>/session-attest/<claude_session_id>/<issued_at>-<launch_seq>.json`.
   - **Permissions**: Directory mode 0700, file mode 0600 written via atomic `wx` temp file and
     rename (`apps/desktop/electron/session-binding-issuer.ts:writeAttestationFile`).
   - **Purpose and verification**: A short-lived credential proving that a specific Claude CLI
     launch (`claude_session_id`) belongs to `hermes_session_id` and is bound to `project_root`
     under `binding_nonce`. **This is the only artifact conductor verifies.**
   - **TTL and refresh**:
     - Maximum TTL is capped at 30 minutes (`ATTEST_MAX_TTL_MS = 1800000` ms in
       `hermes_owner_grant/attest.py:35` and `apps/desktop/electron/session-binding-issuer.ts:31`).
       The verifier rejects any file where `expires_at - issued_at > 30 min` (`ttl_exceeded`).
     - Electron main's issuer long-polls the backend launch table (`GET /api/session-launches?since=<seq>&wait=2`
       in `hermes_cli/web_routers/sessions.py:539-556`). Before spawning a Claude CLI instance, the
       backend records the planned Claude session ID and waits up to 1.5 seconds for main to write
       the signed attestation file. Main refreshes live attestations at TTL/2 (every 15 minutes,
       `ATTEST_REFRESH_INTERVAL_MS = 900000` ms) while the session remains active in the launch feed
       (`session-binding-issuer.ts`).
   - **Payload schema**:
     ```json
     {
       "v": 1,
       "aud": ["conductor:session-binding"],
       "owner_uid": 501,
       "profile": "default",
       "backend": "b_123",
       "hermes_session_id": "hs_01JF...",
       "claude_session_id": "11111111-2222-4333-8444-555555555555",
       "launch_seq": 1,
       "project_root": "/abs/path/to/worktree",
       "repo_common_root": "/abs/path/to/repo",
       "binding_nonce": "MFRGGZDFMZTWQ2LK",
       "binding_seq": 1,
       "repo_remote": "git@github.com:example/repo.git",
       "hermes_lineage": ["hs_root_01"],
       "issued_at": 1759140000000,
       "expires_at": 1759141800000
     }
     ```
     When a session is unbound, `project_root`, `repo_common_root`, `repo_remote`, `binding_nonce`,
     and `binding_seq` are `null`.

### Lineage across compression

When an SDK session undergoes manual context compression (`/compress`), Hermes rotates the session
context to a child session ID (`agent/conversation_compression.py`, `hermes_state_sessions.py`).
To prevent build ownership from breaking across compression boundaries, the backend tracks the
session's lineage: an ordered list of ancestor session IDs `[root_session_id, ..., parent_session_id]`.

- **Runtime memory only**: Lineage is recorded strictly in the backend's in-memory runtime history
  (`claude_sdk_launch_table` / `claude_sdk_runtime_session.py`). It is **never** read from
  `state.db`'s `parent_session_id` column, which is agent-writable.
- **Entry cap**: Lineage arrays are capped at `MAX_LINEAGE_ENTRIES = 16`
  (`hermes_owner_grant/attest.py:37`).
- **Resolver restamping**: The attestation includes `hermes_lineage`. Conductor's resolver accepts a
  launch if the marker's recorded `hermes_session_id` matches `attestation.hermes_session_id` OR
  any ancestor in `attestation.hermes_lineage`. When matched via lineage, conductor updates the
  marker's `hermes_session_id` to the current child session ID in an audited rebind.

### Unbind semantics and confirmation

- **Owner action**: An unbind is triggered explicitly in the UI via the titlebar pill picker or
  the session row menu ("Unbind").
- **Native confirmation for live builds**: If the session has an active, live-attested build
  (`session-binding-issuer.ts:isAttestationLive`), unbinding or re-binding prompts the owner with a
  native macOS confirmation dialog (`session-binding-ipc.ts:confirmRebind`, decision O2).
- **Immediate revocation**: Main increments `seq`, marks the binding record as `state: "unbound"`
  with null project fields, and immediately issues a fresh attestation with `project_root: null`
  and `binding_nonce: null` for all live Claude session IDs of that Hermes session
  (`session-binding-issuer.ts:revoke`).
- **Revocation timeline**:
  - *Honest case*: The null attestation is written immediately. When conductor evaluates it,
    `attest.py` detects null binding fields and returns `ok: false`, reason `unbound` (exit 1).
  - *Adversarial case*: If an agent deletes the newest attestation file or blocks updates, the
    previous valid attestation remains valid only until its signed `expires_at` timestamp. Because
    attestations are hard-capped at 30 minutes, unbind delay is bounded by **at most one TTL
    (30 minutes)** (accepted design decision O4).

### Environment variable hints and scope limits

- **Environment variables are diagnostic hints only**:
  - `HERMES_SESSION_ID`: Exported per session by Hermes at CLI spawn
    (`agent/transports/claude_agent_sdk_session_config.py:163-174`), but agent Bash processes can
    prefix or alter process environment variables.
  - `HERMES_SESSION_ATTEST_DIR`: Exported as `<grants_dir>/session-attest` as a diagnostic hint
    (`claude_agent_sdk_session_config.py:185-190`).
  - `TB_STATE_ROOT`: Set to `binding.project_root` for bound sessions only after verifying main's
    signed binding record using `hermes_owner_grant.attest.verify_binding_file`
    (`claude_agent_sdk_session_config.py:209-266`). Falls back to `~/.hermes/sdk-state/<sha256(cwd)[:16]>`
    when unbound.
  - Conductor must never trust environment variables to resolve session identity or build ownership.
    Conductor reads `claude_session_id` from trusted hook stdin, looks up the attestation under that
    directory, and extracts `hermes_session_id` from the signed payload. Env `HERMES_SESSION_ID` must
    match the attestation; any discrepancy fails closed.
  - Marker rebinds must **only run within hook execution context**, never from Bash-invoked commands
    or verbs, preventing an agent from spoofing environment variables to alter build ownership.
- **Backend scope limits (Decision O5)**:
  - Launch attestations are signed and issued **only for app-spawned backends** where Electron main
    holds the authenticated long-poll connection (`apps/desktop/electron/session-binding-issuer.ts`).
  - Terminal-launched backends (`hermes` run directly in a shell) and remote backends are **out of
    scope for b10**. They receive no launch attestations and continue to rely on legacy marker lease
    and dead-owner rules. Remote backend attestation is planned for b11.

### Threat model and security boundaries

- **User UID is agent-writable**: All files and directories under the user's UID (including
  `~/.hermes`, `state.db`, and `<grants_dir>`) are agent-writable. Only the root anchor
  (`/Library/Application Support/Hermes/owner-grant/anchor.json`) and the installed verifier
  package are root-owned. Signatures provide data integrity and provenance, but cannot prevent
  file deletion or local tampering.
- **Anchor-pinned storage**: Attestations and bindings reside strictly within subdirectories of the
  anchor-pinned `grants_dir` (`session-bindings/<profile>/` and `session-attest/<claude_session>/`).
  No CLI flag or environment variable can repoint these paths. Files are written with 0700
  directories, 0600 file modes, `wx` exclusive creation, symlink checks (`symlink_refused`), and
  atomic rename.
- **Sibling replay protection**: Launch attestations are keyed by and explicitly bind
  `claude_session_id`. Because hook stdin is provided by the Claude runtime, a sibling Session B
  cannot present Session A's attestation as its own.
- **Rollback resistance**:
  - Replaying an old attestation presents a dead `claude_session_id` that does not match the active
    session.
  - Conductor enforces monotonic, forward-only rebinds (`launch_seq` or `issued_at` strictly newer).
  - Main maintains in-memory `seq` high-water marks per session during its lifetime.
  - Main accepts only active-kid binding records upon restart; a leaked or retired key cannot seed
    or roll back a binding.
- **Bounded unbind exposure**: Because there is no consume ledger for attestations, deleting a
  revocation attestation can extend authority by at most one TTL (≤30 minutes).

## Verifier CLI

The supported operations are `verify`, `list`, `verify-attestation`, and `anchor-status`; `hermes owner doctor`
reports local installation health. For a hook, use the installed single-file verifier at
this fixed, root-owned path and interpreter:

```sh
/usr/bin/python3 -I -S "/Library/Application Support/Hermes/owner-grant/hermes_owner_verify.py" verify \
  --session "$HERMES_SESSION_ID" --claude-session "$HOOK_STDIN_SESSION_ID" \
  --scope conductor:gate:job-store-write-block --text-sha "$PROMPT_SHA256"
```

The path is pinned by the hook implementation. Never accept a verifier path, session id, or
Claude session id from agent-supplied arguments. Derive `--session` from the trusted
`HERMES_SESSION_ID` hook environment and `--claude-session` from the hook's trusted stdin
JSON's `session_id` field. For a quote match, use `--quote-stdin` and pipe the quote; never put quote text in argv.
An agent may supply the claimed scope and a grant id, hash, or quote, but the hook supplies
the trusted session identities and required scopes. Prefer the pinned `-I -S` command over
`hermes owner verify`: the latter is a human convenience wrapper in agent-writable code.

The verifier writes one JSON object to stdout. `verify` returns the following schema:

```json
{
  "schema": "hermes-owner-verify/v1",
  "ok": false,
  "reason": "not_found",
  "detail": null,
  "tier": "signed",
  "grant": null,
  "match": null,
  "consumed": null,
  "candidates": 0,
  "audit": false,
  "checked_at": 0,
  "verifier": {"version": "1.0.0", "impl": "pure", "anchor_sha256": null}
}
```

Treat every non-zero result as deny, and authorize only on exit 0. `grant` and `match` are
populated only after the corresponding verification stages pass. Exit codes are:

| Code | Name | Meaning |
| ---: | --- | --- |
| 0 | ok | Valid grant or successful anchor-status check |
| 1 | deny | Grant does not authorize this request |
| 2 | usage | Invalid command or request |
| 3 | anchor | Anchor missing or untrusted; report this to the owner |
| 4 | not_found | No matching valid grant |
| 5 | internal | Verifier could not complete safely |
| 6 | evidence | Evidence only, not authorizing: a pass under `--audit-at` or `--allow-fragment` |

Exit 6 keeps `ok: true` and the `match.kind` (`fragment`) and `audit` fields so audit tools
can display the evidence, but it is evidence only and never authorizes. A hook must key on
the exit code, not on `ok`.

A quote matches as `exact` (the whole owner text) or `segment` (one or more whole
consecutive sentences, at least 12 characters). Sentences end only at `.`, `!` or `?`
followed by whitespace and a capital or digit; a line break is whitespace inside a
sentence, and semicolons, colons, dashes, ellipses, abbreviations and initials never end
one. A paragraph break or list item starts a new segment only after a finished sentence,
and a list introduced by an unfinished line (such as `Do NOT:`) stays one segment with it.
Anything else inside the owner text is a refused fragment (`quote_fragment`).

### Launch attestation CLI (verify-attestation)

To verify a launch attestation, invoke `verify-attestation` using the installed, root-owned
verifier:

```sh
/usr/bin/python3 -I -S "/Library/Application Support/Hermes/owner-grant/hermes_owner_verify.py" verify-attestation \
  --claude-session-stdin --session "$HERMES_SESSION_ID"
```

Or via module invocation in Python:

```sh
python3 -m hermes_owner_grant.cli verify-attestation \
  --claude-session "$HOOK_STDIN_CLAUDE_SESSION_ID" --session "$HERMES_SESSION_ID"
```

Arguments (`hermes_owner_grant/cli.py:81-85`):
- `--claude-session <id>`: The Claude session ID to look up in `<grants_dir>/session-attest/<id>/`.
- `--session <id>`: Optional expected Hermes session ID. When passed, verification fails with
  `session_mismatch` if `attestation.hermes_session_id` differs.
- `--claude-session-stdin`: Read the Claude session ID from standard input. If standard input is a
  JSON object (such as Claude hook stdin), the CLI parses it and extracts `claude_session_id` or
  `session_id`. If `--session` was not specified on the command line, it also extracts
  `hermes_session_id` from the JSON object. If input is raw text, it is trimmed and used directly as
  the Claude session ID.

`verify-attestation` returns a single JSON object to stdout matching schema `hermes-owner-verify/v1`:

```json
{
  "schema": "hermes-owner-verify/v1",
  "ok": true,
  "reason": null,
  "detail": null,
  "hermes_session_id": "hs_01JF...",
  "claude_session_id": "11111111-2222-4333-8444-555555555555",
  "project_root": "/Users/user/repo",
  "repo_common_root": "/Users/user/repo",
  "binding_nonce": "MFRGGZDFMZTWQ2LK",
  "hermes_lineage": ["hs_root_01"],
  "issued_at": 1759140000000,
  "expires_at": 1759141800000,
  "candidates": 1,
  "checked_at": 1759140005000,
  "verifier": {
    "version": "1.0.0",
    "impl": "pure",
    "anchor_sha256": "abcdef..."
  },
  "attestation": {
    "v": 1,
    "aud": ["conductor:session-binding"],
    "owner_uid": 501,
    "profile": "default",
    "backend": "b_123",
    "hermes_session_id": "hs_01JF...",
    "claude_session_id": "11111111-2222-4333-8444-555555555555",
    "launch_seq": 1,
    "project_root": "/Users/user/repo",
    "repo_common_root": "/Users/user/repo",
    "binding_nonce": "MFRGGZDFMZTWQ2LK",
    "binding_seq": 1,
    "repo_remote": "git@github.com:example/repo.git",
    "hermes_lineage": ["hs_root_01"],
    "issued_at": 1759140000000,
    "expires_at": 1759141800000
  }
}
```

Exit codes for `verify-attestation` match the verifier exit codes above (codes 0 through 5):
- **0 (`ok`)**: Valid launch attestation verified under active anchor key.
- **1 (`deny`)**: Verification refused: signature invalid, expired, ttl_exceeded, wrong_audience, uid_mismatch, session_mismatch, claude_session_mismatch, or explicitly unbound (`unbound`).
- **2 (`usage`)**: Invalid arguments or missing session IDs.
- **3 (`anchor`)**: Anchor missing or untrusted.
- **4 (`not_found`)**: No candidate attestation files found for this Claude session ID.
- **5 (`internal`)**: Internal verifier error.

## Conductor contract

> **Never run the verifier through `/dev/fd/N` (or any fd path).** On macOS, `/usr/bin/python3 -I -S /dev/fd/N` exits 0 without running the script. Exec only the installed absolute path, `/Library/Application Support/Hermes/owner-grant/hermes_owner_verify.py`, and authorize only when the exit code is 0 **and** stdout parses as the verifier's JSON result with `ok: true`. A verifier that never ran prints nothing, so it can't pass.

Conductor calls the verifier and consumes its result. It must not reimplement Ed25519,
signature parsing, or grant validation. It must not trust `revoked.jsonl` as the only
revocation control: the anchor's key status and the verifier's TTL, subject, signature and
session checks remain authoritative. Revocation entries are agent-writable, so additions may
deny service but cannot grant authority.

Local text capture is disabled inside Hermes SDK sessions. Peer, widget, wake and digest
messages can arrive as user prompts, so no text marker proves the owner typed them. In those
sessions only signed grants count. A `tier: "local"` capture must never satisfy `marker:bypass`
or `prod:*`.

### Addendum §7 amendments

Conductor's consumer changes are:

1. Remove reads of `~/.hermes/owner-grant.pub` and any in-conductor crypto; call the pinned verifier.
2. Bind session identities from trusted hook context, never verb arguments.
3. Pass every required scope; prod passes a subject and `--consume`, and marker passes `--consume`.
4. For `owner_line` in Hermes sessions, use `--quote-stdin` or `--text-sha`; accept only
   `match.kind` `exact`, `segment`, or `sha`.
5. Disable local capture in Hermes sessions, mark remaining captures `tier: "local"`, and refuse
   them for `marker:bypass` and `prod:*`.
6. Map the exit codes above; surface code 3 as “owner-grant anchor not installed or tampered”.
7. Stop maintaining a separate consumed ledger. Appending to `revoked.jsonl` is allowed because
   it only reduces authority.
8. Add new scope names and labels to `hermes_owner_grant/scopes.json`.
9. Consider root-owning conductor gate hooks through Claude Code managed settings; this hardening
   remains optional and unverified.

### Session binding contract

When resolving project workspace ownership for a session, conductor interacts with the launch
attestation verifier under the following contract:

1. **Call the pinned verifier**: Invoke the installed verifier path
   (`/Library/Application Support/Hermes/owner-grant/hermes_owner_verify.py verify-attestation`) with
   `--claude-session-stdin` and `--session "$HERMES_SESSION_ID"`. Conductor must not reimplement
   Ed25519 signature checks or re-parse attestation envelopes.
2. **Hook stdin for session identity**: Pipe Claude hook stdin directly to the verifier. Conductor
   must extract `claude_session_id` from trusted hook stdin, not from environment variables or
   command-line arguments. `hermes_session_id` must be read from the verified attestation output
   (`result.hermes_session_id`). Env `HERMES_SESSION_ID` must match; any discrepancy fails closed.
3. **Pending vs Denial semantics**:
   - **Exit code 4 (`not_found`)**: Must be treated as **`pending`**, NEVER as a refusal. During
     session startup, Claude CLI hooks may fire before or while Electron main writes the attestation
     file (the backend enforces a 1.5-second synchronization barrier, but slow startup or polling
     can cause hooks to observe an empty directory). Defer verification or retry.
   - **Exit code 1 (`deny`)**: Treat as a refusal (attestation expired, TTL exceeded, signature
     invalid, session mismatch, or explicitly unbound with `reason: "unbound"`).
   - **Exit code 3 (`anchor`)**: Report anchor missing or untrusted.
   - **Authorize only on exit code 0**: Accept the binding only when exit code is 0 **and** stdout
     JSON reports `ok: true`.
4. **Project workspace matching**: Compare `result.project_root` (the worktree toplevel) or
   `result.repo_common_root` against the build marker's expected project context. Do not match
   against a cwd-hashed state root.
5. **Lineage resolution across `/compress`**: If `marker.hermes_session_id != result.hermes_session_id`,
   check whether `marker.hermes_session_id` exists in `result.hermes_lineage`. If present, accept the
   lineage match and re-stamp the marker with the current child `result.hermes_session_id` in an
   audited forward rebind.
6. **Forward-only rebinds**: When updating an existing marker, verify that `result.launch_seq` is
   greater than, or `result.issued_at` is strictly newer than, the marker's recorded values for the
   same `hermes_session_id` and `binding_nonce`.
7. **Hook context only**: Execute marker mutations and ownership rebinds exclusively within hook
   execution context. Never allow rebind operations from Bash-invoked verbs or tools.
8. **Verifier reinstall prerequisite**: The `verify-attestation` command is present in the installed
   verifier only after the owner completes the post-upgrade verifier reinstall (see
   [Reinstall after an upgrade](#reinstall-after-an-upgrade)). Prior to reinstall, calling
   `verify-attestation` exits with code 2 (`usage`).

## Reinstall after an upgrade

The installed verifier is a root-owned copy of `hermes_owner_grant`, including the scope
catalog and launch attestation verifier (`attest.py`), so new scopes, subject grammar, and
the `verify-attestation` command reach hooks only after it is reinstalled. After
upgrading to a build that changes the verifier package, open Settings → Gateways → **Let
conductor verify owner decisions** (the owner-grant row) and click **Rotate key**. macOS asks for
an admin password once; the app re-pushes the verifier package whenever the installed package
hash differs from the bundled one, not only when the launcher changed. Until then the old
verifier refuses the new scopes and commands: it does not know their classes or subcommands,
so a request naming one is a usage error (exit 2).

## Trust limits

The anchor and installed verifier must be root-owned and not writable by group or other users;
the verifier also checks the ownership and permissions of every directory above the anchor.
The grants directory is intentionally writable by the user running Hermes. A development
checkout does not protect the signing key from an agent with shell access; a packaged, signed
Hermes build is required for that stronger claim.
