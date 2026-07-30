"""Human-in-the-loop approval gate for provisioning actions (H4).

The provisioning agent must never trigger a real infrastructure build purely on
model output. This module enforces an approval check *in code* — independent of
the system prompt — so that a prompt-injection or model error cannot bypass it.

How it works
------------
Every execution path that provisions infrastructure calls
``require_approval(action, details, approval_token)`` before doing any work.
The call fails closed (raises ``ApprovalRequired``) unless a valid approval
token is supplied for the exact action being executed.

Approval tokens are minted out-of-band by a human-facing control (the portal's
"Approve" button, a JIRA transition, a ChatOps command, etc.) via
``issue_approval_token`` and are:

  * bound to the specific action + a hash of the plan details, so a token for
    one plan cannot approve a different one;
  * single-use and time-limited (default 15 minutes).

For local development the gate can be disabled by setting
``REQUIRE_HUMAN_APPROVAL=false``; it defaults to enabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

# Enabled by default — must be explicitly turned off for local dev.
REQUIRE_HUMAN_APPROVAL = os.getenv("REQUIRE_HUMAN_APPROVAL", "true").lower() != "false"

# Secret used to sign approval tokens. In production this is sourced from
# Secrets Manager / SSM; a per-process default is used only for local dev.
_APPROVAL_SECRET = os.getenv("APPROVAL_SIGNING_SECRET", "")

_TOKEN_TTL_SECONDS = int(os.getenv("APPROVAL_TOKEN_TTL_SECONDS", "900"))

# Tracks tokens already redeemed so each approval is single-use (per process;
# back this with DynamoDB/Redis for a multi-replica deployment).
_REDEEMED: set[str] = set()


class ApprovalRequired(Exception):
    """Raised when a provisioning action is attempted without valid approval."""


def _plan_fingerprint(action: str, details: dict) -> str:
    """Stable hash binding a token to one specific action + plan."""
    payload = json.dumps({"action": action, "details": details}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _signing_secret() -> str:
    if _APPROVAL_SECRET:
        return _APPROVAL_SECRET
    if REQUIRE_HUMAN_APPROVAL:
        # Fail loudly rather than silently accepting unsigned tokens.
        raise ApprovalRequired(
            "APPROVAL_SIGNING_SECRET is not configured; cannot verify approvals."
        )
    return "local-dev-only"


def issue_approval_token(action: str, details: dict, approver: str) -> str:
    """Mint a signed, time-limited approval token for a specific plan.

    Call this from the human-facing approval control once a person has reviewed
    and approved the plan. The returned token is passed back into the execution
    path (``approval_token``) to authorize exactly this action.
    """
    fingerprint = _plan_fingerprint(action, details)
    expires_at = int(time.time()) + _TOKEN_TTL_SECONDS
    body = f"{fingerprint}:{expires_at}:{approver}"
    signature = hmac.new(
        _signing_secret().encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    return f"{body}:{signature}"


def require_approval(action: str, details: dict, approval_token: str | None) -> str:
    """Enforce that ``action`` on ``details`` has valid human approval.

    Returns the approver identity on success (for audit logging). Raises
    ``ApprovalRequired`` otherwise. Fails closed on any malformed/expired/reused
    token or fingerprint mismatch.
    """
    if not REQUIRE_HUMAN_APPROVAL:
        return "approval-gate-disabled(local-dev)"

    if not approval_token:
        raise ApprovalRequired(
            f"Action '{action}' requires human approval. No approval token was "
            f"supplied. Obtain approval before executing this provisioning step."
        )

    try:
        fingerprint, expires_str, approver, signature = approval_token.rsplit(":", 3)
        expires_at = int(expires_str)
    except (ValueError, AttributeError) as exc:
        raise ApprovalRequired("Malformed approval token.") from exc

    expected_body = f"{fingerprint}:{expires_at}:{approver}"
    expected_sig = hmac.new(
        _signing_secret().encode(), expected_body.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, signature):
        raise ApprovalRequired("Approval token signature is invalid.")

    if time.time() > expires_at:
        raise ApprovalRequired("Approval token has expired; request re-approval.")

    if fingerprint != _plan_fingerprint(action, details):
        raise ApprovalRequired(
            "Approval token does not match this plan — the request changed after "
            "approval. Re-approve the current plan."
        )

    if approval_token in _REDEEMED:
        raise ApprovalRequired("Approval token has already been used (single-use).")
    _REDEEMED.add(approval_token)

    return approver
