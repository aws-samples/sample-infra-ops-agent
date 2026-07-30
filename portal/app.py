"""Provisioning Portal — self-service web application.

A minimal FastAPI application that serves as the front-end portal described in
the blog post. In production this runs on Amazon ECS with AWS Fargate behind an
Application Load Balancer.

Start for development:
    uvicorn portal.app:app --reload --port 8000
"""

import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agents.handoff import ProvisioningRequirements, perform_handoff
from runtime.requirement_gathering_runtime import handler as requirement_handler
from runtime.provisioning_runtime import handler as provisioning_handler
from tools.jira_tools import lookup_ticket

app = FastAPI(
    title="Infrastructure Provisioning Portal",
    description="Self-service provisioning portal backed by Amazon Bedrock AgentCore",
    version="1.0.0",
)

STATIC_DIR = Path(__file__).parent / "static"

# Known demo tickets served by the JIRA instance. In production this would be
# a JQL search (e.g. project = INFRA AND status != Done).
DEMO_TICKET_IDS = ["INFRA-1234", "INFRA-1235", "INFRA-1236"]


class ChatRequest(BaseModel):
    """A user message sent to the Requirement Gathering Agent."""

    session_id: Optional[str] = None
    message: str
    ticket_id: Optional[str] = None


class ChatResponse(BaseModel):
    """The agent's response to a user message."""

    session_id: str
    response: str
    turn_count: int
    stage: str
    handoff_ready: bool = False


class ProvisionRequest(BaseModel):
    """A provisioning execution request sent to the Provisioning Agent."""

    session_id: Optional[str] = None
    requirements: dict


class ProvisionResponse(BaseModel):
    """The Provisioning Agent's execution outcome."""

    session_id: str
    response: str
    ticket_id: Optional[str] = None
    stage: str


@app.get("/")
def serve_ui():
    """Serve the provisioning portal UI."""
    return FileResponse(STATIC_DIR / "index.html")


class CreateTicketRequest(BaseModel):
    """Request body for creating a new JIRA provisioning ticket."""

    summary: str
    description: str = ""
    priority: str = "Medium"
    assignee: str = "Unassigned"
    environment: str = ""
    project_type: str = ""
    requested_resources: str = ""


@app.post("/api/tickets")
def create_ticket(request: CreateTicketRequest):
    """Create a new provisioning ticket in JIRA."""
    import requests as http_client
    from config import JIRA_BASE_URL

    body = {
        "fields": {
            "summary": request.summary,
            "description": request.description,
            "priority": {"name": request.priority},
            "assignee": {"displayName": request.assignee},
            "project": {"name": "Infrastructure"},
            "customfield_10001": request.environment,
            "customfield_10002": request.project_type,
            "customfield_10003": request.requested_resources,
        }
    }

    try:
        resp = http_client.post(
            f"{JIRA_BASE_URL}/rest/api/3/issue/",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"JIRA error: {e}")

    # Add the new ticket to our known list so it shows on the dashboard
    DEMO_TICKET_IDS.append(result["key"])

    return result


@app.get("/health")
def health_check():
    """Health check endpoint for ALB target group."""
    return {"status": "healthy"}


@app.get("/api/tickets")
def list_tickets():
    """List provisioning tickets from JIRA for the dashboard view."""
    import requests as http_client
    from config import JIRA_BASE_URL

    tickets = []
    for ticket_id in DEMO_TICKET_IDS:
        try:
            resp = http_client.get(
                f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_id}",
                timeout=10,
            )
            resp.raise_for_status()
            issue = resp.json()
            fields = issue["fields"]
            tickets.append(
                {
                    "ticket_id": issue["key"],
                    "summary": fields.get("summary", ""),
                    "status": fields.get("status", {}).get("name", "Unknown"),
                    "priority": fields.get("priority", {}).get("name", "Medium"),
                    "assignee": fields.get("assignee", {}).get("displayName", "Unassigned"),
                    "project": fields.get("project", {}).get("name", ""),
                    "description": fields.get("description", ""),
                    "created": fields.get("created", ""),
                    "environment": fields.get("customfield_10001", ""),
                    "project_type": fields.get("customfield_10002", ""),
                    "requested_resources": fields.get("customfield_10003", ""),
                }
            )
        except Exception as e:
            tickets.append(
                {
                    "ticket_id": ticket_id,
                    "summary": f"(failed to load: {e})",
                    "status": "Unknown",
                    "priority": "Medium",
                    "assignee": "—",
                    "project": "—",
                    "description": "",
                    "created": "",
                }
            )
    return tickets


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    """Send a message to the Requirement Gathering Agent.

    Creates a new session if session_id is not provided. Returns the agent's
    response and session metadata.
    """
    session_id = request.session_id or str(uuid.uuid4())

    event = {
        "session_id": session_id,
        "user_message": request.message,
        "ticket_id": request.ticket_id,
    }

    result = requirement_handler(event, {})
    return ChatResponse(
        session_id=result["session_id"],
        response=result["response"],
        turn_count=result["turn_count"],
        stage=result["stage"],
        handoff_ready=result.get("handoff_ready", False),
    )


@app.post("/api/provision", response_model=ProvisionResponse)
def provision(request: ProvisionRequest):
    """Execute a provisioning plan through the Provisioning Agent.

    Accepts a validated requirements payload (typically produced by the handoff
    from the Requirement Gathering Agent) and triggers the IaC pipeline.
    """
    session_id = request.session_id or str(uuid.uuid4())

    event = {
        "session_id": session_id,
        "requirements": request.requirements,
    }

    result = provisioning_handler(event, {})

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return ProvisionResponse(
        session_id=result["session_id"],
        response=result["response"],
        ticket_id=result.get("ticket_id"),
        stage=result["stage"],
    )


@app.post("/api/handoff")
def execute_handoff(requirements: ProvisioningRequirements):
    """Validate and execute the handoff between agents.

    Takes a ProvisioningRequirements object, validates completeness, and
    returns the serialized payload for the Provisioning Agent.
    """
    try:
        result = perform_handoff(requirements)
        return result
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
