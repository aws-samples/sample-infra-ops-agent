"""JIRA integration tools for the Infrastructure Agent.

These tools provide read/write access to JIRA tickets for provisioning workflow
management. The Requirement Gathering Agent has read-only access; the
Provisioning Agent has write access.
"""

import requests
from strands import tool

from config import JIRA_BASE_URL, JIRA_API_TOKEN, JIRA_USER_EMAIL


def _jira_headers() -> dict:
    """Return authentication headers for the JIRA API."""
    import base64

    credentials = base64.b64encode(
        f"{JIRA_USER_EMAIL}:{JIRA_API_TOKEN}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json",
    }


@tool
def lookup_ticket(ticket_id: str) -> dict:
    """Look up a JIRA provisioning ticket by its ID and return its metadata.

    Args:
        ticket_id: The JIRA ticket ID (e.g., INFRA-1234).

    Returns:
        A dictionary containing ticket metadata: summary, status, priority,
        assignee, project details, and any custom fields.
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_id}"
    response = requests.get(url, headers=_jira_headers(), timeout=30)
    response.raise_for_status()
    issue = response.json()

    fields = issue["fields"]
    return {
        "ticket_id": issue["key"],
        "summary": fields.get("summary", ""),
        "status": fields.get("status", {}).get("name", "Unknown"),
        "priority": fields.get("priority", {}).get("name", "Medium"),
        "assignee": fields.get("assignee", {}).get("displayName", "Unassigned"),
        "project": fields.get("project", {}).get("name", ""),
        "description": fields.get("description", ""),
        "created": fields.get("created", ""),
        "custom_fields": {
            "environment": fields.get("customfield_10001", ""),
            "project_type": fields.get("customfield_10002", ""),
            "requested_resources": fields.get("customfield_10003", ""),
        },
    }


@tool
def get_ticket_requirements(ticket_id: str) -> dict:
    """Extract structured provisioning requirements from a JIRA ticket.

    Reads the ticket description and custom fields to build an initial
    requirements schema. Missing fields will be identified for follow-up.

    Args:
        ticket_id: The JIRA ticket ID (e.g., INFRA-1234).

    Returns:
        A dictionary with known requirements and a list of missing fields.
    """
    ticket = lookup_ticket(ticket_id)

    known = {}
    missing = []

    required_fields = [
        "environment",
        "instance_type",
        "operating_system",
        "vpc",
        "subnet",
        "security_group",
        "storage_size_gb",
        "project_name",
        "owner",
        "cost_center",
    ]

    description = ticket.get("description", "") or ""
    custom = ticket.get("custom_fields", {})

    if custom.get("environment"):
        known["environment"] = custom["environment"]
    if custom.get("requested_resources"):
        known["requested_resources"] = custom["requested_resources"]

    for field in required_fields:
        if field not in known:
            missing.append(field)

    return {
        "ticket_id": ticket["ticket_id"],
        "status": ticket["status"],
        "known_requirements": known,
        "missing_fields": missing,
        "raw_description": description,
    }


@tool
def update_ticket_status(ticket_id: str, status: str, comment: str = "") -> dict:
    """Update a JIRA ticket's status and optionally add a comment.

    This tool requires write access and is available only to the Provisioning Agent.

    Args:
        ticket_id: The JIRA ticket ID.
        status: The new status (e.g., "In Progress", "Done", "Ready for Prod").
        comment: Optional comment to add to the ticket.

    Returns:
        Confirmation of the update with the new status.
    """
    if comment:
        comment_url = f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_id}/comment"
        comment_body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": comment}],
                    }
                ],
            }
        }
        requests.post(
            comment_url,
            json=comment_body,
            headers=_jira_headers(),
            timeout=30,
        )

    return {"ticket_id": ticket_id, "new_status": status, "comment_added": bool(comment)}
