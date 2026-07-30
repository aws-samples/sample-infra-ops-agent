"""AWX (Ansible Tower) integration tools for the Provisioning Agent.

These tools invoke AWX job templates to execute the IaC pipelines
(Terraform, Ansible, Python, PowerShell) that provision infrastructure.
"""

import time

import requests
from strands import tool

from config import AWX_BASE_URL, AWX_API_TOKEN
from tools.approval import ApprovalRequired, require_approval


def _awx_headers() -> dict:
    """Return authentication headers for the AWX API."""
    return {
        "Authorization": f"Bearer {AWX_API_TOKEN}",
        "Content-Type": "application/json",
    }


@tool
def invoke_awx_template(
    template_name: str, extra_vars: dict, approval_token: str | None = None
) -> dict:
    """Launch an AWX job template with the given variables.

    This triggers the IaC pipeline mapped to the provisioning plan. The
    template_name must match a registered AWX job template.

    H4 (human-in-the-loop): this call is gated in code. Before any job is
    launched, ``require_approval`` verifies a valid, plan-bound approval token.
    Without approval the launch fails closed and returns an error — the model
    cannot provision infrastructure on its own, regardless of prompt content.

    Args:
        template_name: Name of the AWX job template to invoke
            (e.g., "Create Linux EC2 Instance", "PreCheck Assessment").
        extra_vars: Dictionary of variables to pass to the playbook. Typically
            includes instance_type, environment, vpc, subnet, hostname, etc.
        approval_token: Signed, single-use approval token minted by the
            human-facing approval control for this exact plan. Required unless
            REQUIRE_HUMAN_APPROVAL is disabled for local development.

    Returns:
        A dictionary with the launched job ID, status URL, and initial state,
        or an ``error``/``approval_required`` field if the gate blocked it.
    """
    # H4: enforce human approval before any real provisioning work.
    try:
        approver = require_approval(
            action=template_name, details=extra_vars, approval_token=approval_token
        )
    except ApprovalRequired as exc:
        return {"approval_required": True, "error": str(exc)}

    # Find the template by name
    search_url = f"{AWX_BASE_URL}/api/v2/job_templates/"
    params = {"name": template_name}
    search_resp = requests.get(
        search_url, headers=_awx_headers(), params=params, timeout=30
    )
    search_resp.raise_for_status()
    results = search_resp.json().get("results", [])

    if not results:
        return {"error": f"Template '{template_name}' not found in AWX."}

    template_id = results[0]["id"]

    # Launch the job
    launch_url = f"{AWX_BASE_URL}/api/v2/job_templates/{template_id}/launch/"
    launch_body = {"extra_vars": extra_vars}
    launch_resp = requests.post(
        launch_url, json=launch_body, headers=_awx_headers(), timeout=60
    )
    launch_resp.raise_for_status()
    job = launch_resp.json()

    return {
        "job_id": job["id"],
        "template_name": template_name,
        "status": job.get("status", "pending"),
        "status_url": f"{AWX_BASE_URL}/api/v2/jobs/{job['id']}/",
        "created": job.get("created", ""),
        "approved_by": approver,  # H4: record who authorized this build (audit)
    }


@tool
def get_build_status(job_id: int) -> dict:
    """Check the current status of an AWX job.

    Args:
        job_id: The AWX job ID returned by invoke_awx_template.

    Returns:
        A dictionary with the job's current status, start time, elapsed
        duration, and any failure details if applicable.
    """
    url = f"{AWX_BASE_URL}/api/v2/jobs/{job_id}/"
    response = requests.get(url, headers=_awx_headers(), timeout=30)
    response.raise_for_status()
    job = response.json()

    result = {
        "job_id": job["id"],
        "template_name": job.get("name", ""),
        "status": job.get("status", "unknown"),
        "started": job.get("started", ""),
        "finished": job.get("finished", ""),
        "elapsed": job.get("elapsed", 0),
    }

    if job.get("status") == "failed":
        result["failure_reason"] = job.get("result_traceback", "Unknown failure")

    return result


@tool
def wait_for_job_completion(job_id: int, timeout_seconds: int = 600) -> dict:
    """Poll an AWX job until it completes or times out.

    Args:
        job_id: The AWX job ID.
        timeout_seconds: Maximum seconds to wait before returning a timeout.

    Returns:
        The final job status and elapsed time.
    """
    start = time.time()
    terminal_states = {"successful", "failed", "error", "canceled"}

    while time.time() - start < timeout_seconds:
        status = get_build_status(job_id)
        if status["status"] in terminal_states:
            return status
        time.sleep(10)

    return {
        "job_id": job_id,
        "status": "timeout",
        "elapsed": timeout_seconds,
        "message": f"Job did not complete within {timeout_seconds} seconds.",
    }
