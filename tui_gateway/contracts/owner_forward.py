"""Owner-forward (``owner.forward``): the owner's confirmed text delivered into other sessions as a real user
turn. Handler: ``tui_gateway/owner_forward.py``. The grant is signed by Electron main after a native confirm;
see ``_ops/plans/HE-OWNER-FORWARD-DESIGN-2026-09-26.md`` §2-§5."""

from __future__ import annotations

from .base import Params, Result
from .registry import method


class OwnerForwardParams(Params):
    """``grant``: base64url (unpadded) of the exact payload bytes main signed. ``signature``: base64url of the
    Ed25519 signature over the domain prefix plus those bytes. ``text`` is the owner's text exactly as
    confirmed; ``targets`` are profile-qualified ``<profile>:<session_id>`` strings equal as a set to the grant's."""

    grant: str
    signature: str
    text: str
    targets: list[str]


class OwnerForwardTargetResult(Result):
    target_session_id: str
    # delivered | queued | resumed-and-delivered | failed:resume | failed:slot_limit | failed:storage | failed:submit
    status: str
    detail: str | None = None


class OwnerForwardResult(Result):
    nonce: str
    results: list[OwnerForwardTargetResult]


method("owner.forward", params=OwnerForwardParams, result=OwnerForwardResult,
       doc="Deliver owner-confirmed text (a signed Owner Gesture Grant) into up to 5 sessions as a user turn.")
