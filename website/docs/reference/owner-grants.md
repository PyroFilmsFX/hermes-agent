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

## Verifier CLI

The supported operations are `verify`, `list`, and `anchor-status`; `hermes owner doctor`
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

## Reinstall after an upgrade

The installed verifier is a root-owned copy of `hermes_owner_grant`, including the scope
catalog, so new scopes and subject grammar reach hooks only after it is reinstalled. After
upgrading to a build that changes the verifier package, open Settings → Gateways → **Let
conductor verify owner decisions** (the owner-grant row) and click **Rotate key**. macOS asks for
an admin password once; the app re-pushes the verifier package whenever the installed package
hash differs from the bundled one, not only when the launcher changed. Until then the old
verifier refuses the new scopes: it does not know their classes, so a request naming one is a
usage error (exit 2).

## Trust limits

The anchor and installed verifier must be root-owned and not writable by group or other users;
the verifier also checks the ownership and permissions of every directory above the anchor.
The grants directory is intentionally writable by the user running Hermes. A development
checkout does not protect the signing key from an agent with shell access; a packaged, signed
Hermes build is required for that stronger claim.
