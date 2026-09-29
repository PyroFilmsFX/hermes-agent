---
sidebar_position: 6
title: "Owner Forward and Verification"
description: "Inject owner-confirmed turns across sessions and set up cryptographic owner verification in Hermes Desktop"
---

# Owner Forward and Verification

Hermes Desktop includes two tightly integrated features designed for multi-session orchestration and verified human authority:

1. **Owner Forward**: A composer and transcript capability that injects your confirmed message into one or more other sessions as an authentic user turn (`role: "user"`). The receiving agent processes the forwarded message as a genuine turn from you, rather than an agent-to-agent message.
2. **Owner Verification (Owner Grants)**: A cryptographic signing system where Hermes Desktop uses an Ed25519 signing key stored in your system Keychain and anchored in a root-owned trust file. Conductor hooks and backend gateways verify these signed grants to ensure that critical actions and forwarded decisions were confirmed by the human owner.

This guide explains how to set up owner verification on your machine, how owner grants work, how to forward messages between sessions in Hermes Desktop, and how to troubleshoot common issues.

:::note Protocol Reference
This page focuses on setup, desktop workflows, and user-facing behavior. For the low-level JSON wire format, verifier CLI flags, and conductor hook integration, see the [Owner grants reference](../reference/owner-grants.md).
:::

---

## Owner Verification (Owner Grants)

### What Owner Grants Authorise

An **Owner Grant** is signed, session-bound evidence of a specific owner decision. Conductor hooks and the Hermes gateway check these grants so they can verify owner approval without carrying private signing keys or implementing custom cryptography.

Every owner grant cryptographically binds:
- The owner's OS user ID (`owner_uid`)
- The origin session and message ID
- The target session ID(s)
- The gateway backend instance binding (`HERMES_OWNER_GRANT_BACKEND`)
- A single-use random nonce and decision ID
- Timestamps (`issued_at` and `deliver_by`, with a maximum delivery lifetime of 60 seconds for forwards)
- The SHA-256 digest and byte length of the approved text
- Any requested conductor scopes and required subjects

Grants carry authority depending on their **scope**:

| Scope Class | Default TTL | Max TTL | Verification Behavior | Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **Quote-only** (no scope) | 7 days | 7 days | Reusable evidence; grants no conductor authority | Standard message forwarding between chats |
| **`allowlist`** / **`gate`** | 12 hours | 72 hours | Reusable; no subject required | Policy gates, allowlisting member profiles |
| **`marker`** | 1 hour | 4 hours | Single-use | Session marker management |
| **`answer`** / **`defer`** / **`override`** / **`gc`** | 1 hour | 4 hours (1h for gc) | Single-use; subject required | Answering conductor stage questions, deferring waves, overriding budgets, pruning lanes |
| **`prod`** / **`policy`** / **`spend`** | 15 minutes | 1 hour | Single-use; subject required; prompts for Touch ID when available | Production deployments, standing policy changes, paid cloud compute |
| **`continuity`** | 15 minutes | 15 minutes | Single-use; subject required; issued only by Desktop main | Rebinding session markers after a backend relaunch |

---

### Setup and Enabling

Owner verification is supported on **macOS**. Because owner verification relies on an operating-system root trust anchor and Apple Keychain storage, it is not available on Linux or Windows.

To enable owner verification:

1. Open **Hermes Desktop**.
2. Navigate to **Settings** (gear icon) → **Gateways**.
3. Locate the row titled **Let conductor verify owner decisions**.
4. Click **Turn on**.

```
Settings → Gateways
┌────────────────────────────────────────────────────────────────────────┐
│ Let conductor verify owner decisions                                   │
│ Signs your approvals so conductor can check they came from you.        │
│ macOS will ask for an admin password once.                 [ Turn on ] │
└────────────────────────────────────────────────────────────────────────┘
```

When you click **Turn on**, Hermes Desktop performs the following sequence:

1. **Key Generation**: Generates a brand-new Ed25519 keypair using Node.js crypto. The key identifier (`kid`) begins with `ok_` followed by the first 16 hexadecimal characters of the SHA-256 hash of the public key.
2. **Key Storage**: Encrypts the private key at rest using Electron's `safeStorage` API, which binds the encryption key to the macOS Keychain ("Safe Storage" item). The encrypted key blob is stored at `~/.hermes/owner-grants/.key/owner-key.v1.enc` with strict file permissions (`0600` file, `0700` directory).
3. **Owner Confirmation**: Displays a native system dialog showing the key ID (`ok_...`) and explaining that a new owner key will be pinned.
4. **Root Trust Anchor Installation**: Executes a privileged helper via macOS `osascript`, requesting administrator approval once. The helper writes the root-owned trust anchor at:
   ```
   /Library/Application Support/Hermes/owner-grant/anchor.json
   ```
   It also installs the root-owned verification script `hermes_owner_verify.py`, the `verifier/hermes_owner_grant/` package, and precompiled bytecode under `pycache/`. All files are owned by `root:wheel` (`0755` directories, `0644` files).
5. **Strict Validation**: Hermes re-reads the installed anchor using strict permission checks (no symlinks, root-owned, non-writable by group/others) to ensure the newly pinned active key matches the staged key.
6. **Active State**: The settings row updates to display:
   ```
   On. Owner key ok_<id>.
   ```
   The buttons change to **Rotate key** and **Revoke key**.

:::tip Agent Security Boundary
Files in `~/.hermes` can be modified by local agents with terminal access. Because of this, the local encrypted key file is **never** treated as a trust root. The only trusted authority is the root-owned anchor at `/Library/Application Support/Hermes/owner-grant/anchor.json`. The gateway and verifier check this root anchor on every verification, preventing an agent from substituting its own key.
:::

---

### Rotating the Key

Key rotation generates a fresh Ed25519 keypair and updates the root anchor while retiring the previous key.

**When to rotate:**
- Periodic security hygiene.
- After upgrading Hermes to a version that includes an updated verifier package or new scope definitions.
- If you suspect an old key might have been exposed.

**Steps to rotate:**

1. Go to **Settings** → **Gateways**.
2. In the **Let conductor verify owner decisions** row, click **Rotate key**.
3. Review the native confirmation dialog:
   ```
   Replace the owner key with ok_<new_id>?
   The current key ok_<old_id> is retired: grants it already signed keep working until they expire.
   ```
4. Enter your macOS administrator password when prompted.

The root anchor retains the old key with `status: "retired"` and records `retired_at`. Grants already signed by the retired key remain valid until their expiration timestamp, while all subsequent signings use the new active key.

---

### Revoking the Key

Key revocation immediately terminates the validity of the active owner key.

**When to revoke:**
- If your device is lost or compromised.
- If you want to immediately halt all pending and future signed conductor actions.

**Steps to revoke:**

1. Go to **Settings** → **Gateways**.
2. Click **Revoke key**.
3. Review the confirmation dialog:
   ```
   Revoke owner key ok_<id>?
   Every grant signed with ok_<id> stops verifying at once, and signing stays off until you turn this on again.
   ```
4. Enter your macOS administrator password when prompted.

The root anchor marks the key with `status: "revoked"`. Every subsequent verification of any grant signed by that key fails immediately with `key_revoked`. Signing stays disabled in Hermes Desktop until you turn the feature on again.

---

## Owner Forward

### How Owner Forward Works

Owner Forward lets you take instructions, code, or directives from one conversation and deliver them into another session as an authentic user prompt.

```
┌─────────────────────────┐          Native System Confirm          ┌─────────────────────────┐
│     Origin Session      │ ───────► (Signs Owner Grant) ─────────► │     Target Session      │
│  (Desktop Chat / /to)   │                                         │   (Real User Turn)      │
└─────────────────────────┘                                         └─────────────────────────┘
```

Key runtime behaviors:
- **Real User Turn**: The target session receives the forwarded message as a user turn (`role: "user"`). In SQLite storage, the turn is recorded with `display_kind: "owner_forward"` and metadata containing the origin session ID, origin title, forward gesture, confirmation mechanism, fanout index, and grant ID.
- **Unforgeable Stamp**: When the gateway's `owner.forward` method verifies the signed grant envelope, it stamps the delivery with an internal `OwnerForwardStamp`. This stamp exists only in gateway process memory and cannot be supplied over JSON-RPC. If a client attempts to pass `_owner_forward` directly to `prompt.submit`, the gateway rejects the call with error code `4125`.
- **Automatic Wake and Resume**: If the destination session is cold or dormant, the gateway automatically wakes and resumes the session database before delivering the message.
- **Queueing During Active Turns**: If the destination session is already running a turn, the forward is queued. It executes automatically as soon as the active turn finishes.
- **Sequential Independent Fanout**: You can forward a single message to up to 5 target sessions simultaneously. Deliveries execute sequentially and independently; a failure on one target does not abort delivery to the remaining targets.

---

### Using Owner Forward in Desktop

Hermes Desktop provides several ways to initiate an owner forward:

#### 1. Composer Target Control ("Send to…")

Beside the model selector in the chat composer bar, you will see a Send icon button (the Forward Target Pill):

1. Click the **Send to…** pill to open the target picker.
2. Search or select a destination session from the list (shows up to 30 recent sessions across all served profiles).
3. Optionally open the **Expires after** sub-menu to adjust the quote-only TTL (default: 7 days).
4. The pill highlights and displays the target session name:
   ```
   [ Send ▾ ]  ──►  [ Send: payments-refactor ▾ ] [ × ]
   ```
5. Type your message in the composer and press **Enter** (or click Send).
6. A native macOS confirmation dialog asks:
   ```
   Send as you?
   Forward to payments-refactor
   ```
7. Confirm the dialog. The message is signed, forwarded, and delivered.
8. **One-Shot Auto-Clear**: Once the message is successfully delivered, the target automatically clears back to the current chat so subsequent messages are not accidentally sent to the other session. You can also click the **×** button at any time to return to sending in the current chat.

#### 2. Message Action Bar ("Forward to…")

You can forward an existing message from your chat history:

1. Hover over any message (assistant, peer, or user) in the transcript.
2. Click the **Forward to…** icon in the message action bar.
3. If you highlighted a specific passage of text within the message before clicking, only the selected text is forwarded. Otherwise, the full message text is used.
4. The **Forward sheet** opens, allowing you to select up to 5 target sessions, adjust optional scopes and TTL, and click **Send…**.

#### 3. Transcript Selection ("Forward")

When reading a long conversation transcript:

1. Highlight any snippet of text in the transcript using your cursor.
2. A floating **Forward** button appears directly above the selection.
3. Click **Forward**. The selected text is inserted into your current composer as a markdown quote block (`> ...`).
4. Select a destination chat via the composer target pill or send it directly.

#### 4. Slash Command `/to`

You can forward directly from the keyboard without touching the mouse:

```bash
/to payments-refactor Please update the Stripe webhook handler to handle invoice.paid events
```

Hermes Desktop parses the session title or ID, opens the confirmation dialog, and forwards the text upon approval.

#### 5. Send as Signed Decision (`⌘⇧↩`)

To sign a decision for the **current** chat rather than forwarding to another session:

- Type your instructions into the composer.
- Press **Cmd+Shift+Enter** (macOS) or **Ctrl+Shift+Enter**.
- This triggers **Send as signed decision** (gesture `composer_signed`), binding your cryptographic owner signature to that turn in the current conversation.

#### 6. Proposal Cards and `:::send-to` Directives

When an agent suggests delegating a task or decision to another session, it can emit a `:::send-to` block or invoke the owner-forward proposal tool:

- A dedicated proposal card renders inline in the chat transcript with the proposed target session and text.
- Click **Review & send…** on the card to open the Forward sheet.
- You can inspect the text, verify the target session, add or remove conductor scopes, and approve the delivery.
- Once sent, the proposal card marks itself as sent (`Sent ✓`) and becomes inert.

---

### Forwarding Rules and Constraints

To protect session integrity and prevent accidental execution, the forward pipeline enforces strict validation rules:

- **Text Only**: Forwards deliver plain text. If images or file attachments are attached to the composer, Hermes displays:
  ```
  A forward sends text only. Remove the attachments first.
  ```
- **No Slash Commands**: Forwarded text cannot begin with `/`. Slash commands must be run directly inside the target session:
  ```
  A forward cannot start with '/': open the target and type commands there
  ```
- **Character Limit**: By default, forwarded messages cannot exceed **8,000 characters** (configurable up to a hard ceiling of 32,000 characters).
- **Target Limit**: A forward can specify at most **5 target sessions**.
- **Saved Sessions Only**: Forwards can only be delivered to saved, stored sessions in SQLite (`state.db`). You cannot forward to a fresh, unsaved draft chat.
- **Self-Target Restriction**: A forward cannot target its own origin session, unless using the `Cmd+Shift+Enter` signed decision shortcut.
- **Secret Scrubbing**: Forwarded text passes through `secrets.mask` before signing. Text containing unmasked secrets cannot be forwarded.

---

## Configuration Reference

You can customize owner forward limits on your backend in `~/.hermes/config.yaml`:

```yaml
owner_forward:
  # Enable or disable the owner.forward RPC handler on this gateway
  enabled: true

  # Maximum character length for forwarded text (default: 8000, max ceiling: 32000)
  max_chars: 8000

  # Maximum number of target sessions per forward (default: 5, max ceiling: 5)
  max_targets: 5

  # Maximum number of forward deliveries allowed per minute (default: 20, max ceiling: 600)
  per_minute: 20
```

:::note Enforcement Ceilings
Configuration options can tighten constraints (e.g. lowering `max_chars` to `4000` or `max_targets` to `2`), but cannot exceed the hard-coded architectural ceilings (`32000` characters, `5` targets, and `600` per minute).
:::

---

## Troubleshooting

### "Owner forwarding is off. Turn on 'Let conductor verify owner decisions' in Settings → Gateways"

- **Cause**: The root trust anchor (`anchor.json`) has not been installed on this machine, or the owner key has not been generated.
- **Resolution**: Open Hermes Desktop, go to **Settings** → **Gateways**, locate **Let conductor verify owner decisions**, and click **Turn on**.

---

### "Owner-grant anchor doesn't match this app's key" (`mismatch`)

- **Cause**: The active key registered in `/Library/Application Support/Hermes/owner-grant/anchor.json` does not match the encrypted key file in `~/.hermes/owner-grants/.key/owner-key.v1.enc`. This can occur if `~/.hermes` was restored from a backup or copied from another machine.
- **Resolution**: Open **Settings** → **Gateways** and click **Turn on** (or re-enable). Enter your administrator password to re-pin a fresh key to the root anchor.

---

### "Owner-grant anchor not installed or tampered" (`untrusted`)

- **Cause**: The trust anchor or its directory hierarchy failed security validation:
  - Permissions are too open (group- or world-writable).
  - File or parent directory is not owned by `root:wheel`.
  - A symlink was introduced in `/Library/Application Support/Hermes/owner-grant/`.
- **Resolution**: Open **Settings** → **Gateways** and toggle the feature off and on, or run the enable flow again. The installer resets ownership to `root:wheel` (`0755` for directories, `0644` for files).

---

### "The owner key was revoked" (`revoked`)

- **Cause**: The active key in the root anchor was marked as revoked.
- **Resolution**: To resume signing, open **Settings** → **Gateways** and click **Turn on** to generate and anchor a new active key.

---

### "A forward sends text only. Remove the attachments first"

- **Cause**: The composer currently contains attached files or images.
- **Resolution**: Remove all media and file attachments from the composer before forwarding.

---

### "Commands run in this chat. Clear the send-to target first"

- **Cause**: You typed a command starting with `/` (such as `/compress` or `/model`) while a forward target was active in the composer.
- **Resolution**: Click the **×** button on the target pill to clear the destination, then run the command in the current session.

---

### "Too many forwards this minute; wait and send again"

- **Cause**: The gateway exceeded its `per_minute` rate limit (default: 20 forwards per minute).
- **Resolution**: Wait a few moments for the rate window to reset, or increase `owner_forward.per_minute` in `config.yaml`.

---

### "Session `<id>` cannot receive forwards"

- **Cause**: The destination session is an internal non-interactive session (such as a one-shot CLI execution or tool run) that does not accept external turns.
- **Resolution**: Select an interactive session (CLI, Desktop, or messaging platform) as the destination.

---

## Related Documentation

- [Hermes Desktop](./desktop.md) — Comprehensive guide to chat, tabs, and desktop workspace features.
- [Multi-Connection Desktop](./multi-connection-desktop.md) — Managing multiple local and remote Hermes gateways from one desktop interface.
- [Sessions](./sessions.md) — Session persistence, lifecycle, and conversation search.
- [Owner Grants Reference](../reference/owner-grants.md) — Protocol specification, Ed25519 envelope schema, and conductor hook verification.
- [Send-To Directive Reference](../reference/send-to-directive.md) — Specification for agent-generated `:::send-to` decision blocks.
