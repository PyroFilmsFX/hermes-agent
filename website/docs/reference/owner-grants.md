# Owner grants

Owner grants are signed, session-bound evidence for a specific owner decision. They let a
conductor hook verify a decision without carrying signing keys or implementing cryptography.
The quote records evidence; only the signed scopes grant authority.

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
single-use state. Grant files and the revocation/consume ledgers are under the grants
directory pinned in the trusted anchor, not under `HERMES_HOME`.

## Scope grammar and policy

Scopes have the exact form `conductor:<class>:<name>`. Class is one of `allowlist`, `gate`,
`marker`, or `prod`; name starts with a lowercase letter or digit and continues with up to
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

TTL values are maxima, not promises that a grant will remain valid. A verifier request may
ask for several scopes; the strictest class limit applies. Prod hooks should bind the subject
to the action digest. Marker and prod checks use `--consume`.

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

Treat every non-zero result as deny. `grant` and `match` are populated only after the
corresponding verification stages pass. Exit codes are:

| Code | Name | Meaning |
| ---: | --- | --- |
| 0 | ok | Valid grant or successful anchor-status check |
| 1 | deny | Grant does not authorize this request |
| 2 | usage | Invalid command or request |
| 3 | anchor | Anchor missing or untrusted; report this to the owner |
| 4 | not_found | No matching valid grant |
| 5 | internal | Verifier could not complete safely |

## Conductor contract

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

## Trust limits

The anchor and installed verifier must be root-owned and not writable by group or other users;
the verifier also checks the ownership and permissions of every directory above the anchor.
The grants directory is intentionally writable by the user running Hermes. A development
checkout does not protect the signing key from an agent with shell access; a packaged, signed
Hermes build is required for that stronger claim.
