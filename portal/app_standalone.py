"""Infrastructure Provisioning Portal — self-contained ECS/Fargate deployment.

Single-file version of the portal that embeds the JIRA ticket store and does
real EC2 provisioning directly via the ECS task IAM role. No external mock
dependencies — everything runs inside the container behind the ALB.

Runs the same UI as portal/static/index.html plus the Bedrock-backed agent.
"""

import json
import os
import re
import time
import uuid

import boto3
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
SUBNET_ID = os.getenv("PROVISION_SUBNET_ID", "")
SG_ID = os.getenv("PROVISION_SG_ID", "")

app = FastAPI(title="Infrastructure Provisioning Portal")

# ─── In-memory JIRA ticket store (self-contained) ───────────────────────────
TICKET_COUNTER = 1237
TICKETS = {
    "INFRA-1234": {
        "ticket_id": "INFRA-1234",
        "summary": "Provision 2 EC2 instances for analytics project",
        "status": "Open", "priority": "High", "assignee": "Jane Doe",
        "project": "Infrastructure",
        "description": "Need Linux instances in dev for the analytics pipeline. Amazon Linux 2023, t3.micro.",
        "created": "2026-07-15T10:00:00.000+0000",
        "environment": "development", "project_type": "Standard Build",
        "requested_resources": "EC2 Linux x2, t3.micro",
    },
    "INFRA-1235": {
        "ticket_id": "INFRA-1235",
        "summary": "Windows Server for MSSQL staging",
        "status": "Analysis", "priority": "Medium", "assignee": "Shabeer VC",
        "project": "Infrastructure",
        "description": "Windows Server 2022 for MSSQL staging. t3.small, staging environment.",
        "created": "2026-07-14T14:30:00.000+0000",
        "environment": "staging", "project_type": "Database Build",
        "requested_resources": "EC2 Windows, t3.small",
    },
    "INFRA-1236": {
        "ticket_id": "INFRA-1236",
        "summary": "Production web servers scale-out",
        "status": "Ready for Prod", "priority": "Critical", "assignee": "Mohith Reddy",
        "project": "Infrastructure",
        "description": "Scale out production web tier. t3.micro, Amazon Linux 2023.",
        "created": "2026-07-13T09:15:00.000+0000",
        "environment": "production", "project_type": "Scale Out",
        "requested_resources": "EC2 Linux x4, t3.micro",
    },
}

TAXONOMY = {
    "instance_types": ["t3.micro", "t3.small", "t3.medium", "t3.large", "m5.large"],
    "operating_systems": ["Amazon Linux 2023", "Ubuntu 22.04", "Windows Server 2022"],
    "environments": ["development", "staging", "production"],
    "required_tags": ["project", "environment", "owner", "cost-center"],
}


# ─── Strands tools ──────────────────────────────────────────────────────────
@tool
def lookup_ticket(ticket_id: str) -> dict:
    """Look up a provisioning ticket by ID.

    Args:
        ticket_id: The ticket ID (e.g., INFRA-1234).
    """
    t = TICKETS.get(ticket_id)
    if not t:
        return {"error": f"Ticket {ticket_id} not found."}
    return t


@tool
def validate_plan(plan: dict) -> dict:
    """Validate a provisioning plan against organizational policies.

    Args:
        plan: Plan with instance_type, environment, operating_system, tags, etc.
    """
    violations = []
    if plan.get("instance_type") and plan["instance_type"] not in TAXONOMY["instance_types"]:
        violations.append(f"Instance type '{plan['instance_type']}' not approved. Allowed: {TAXONOMY['instance_types']}")
    if plan.get("operating_system") and plan["operating_system"] not in TAXONOMY["operating_systems"]:
        violations.append(f"OS '{plan['operating_system']}' not approved.")
    if plan.get("environment") and plan["environment"] not in TAXONOMY["environments"]:
        violations.append(f"Environment '{plan['environment']}' not valid.")
    tags = plan.get("tags", {})
    missing = [t for t in TAXONOMY["required_tags"] if t not in tags]
    if missing:
        violations.append(f"Missing required tags: {missing}")
    if not (plan.get("encryption_enabled") or plan.get("encryption_at_rest")):
        violations.append("Encryption at rest is required.")
    return {"valid": len(violations) == 0, "violations": violations}


@tool
def validate_hostname(hostname: str) -> dict:
    """Validate a hostname against the naming convention.

    Args:
        hostname: Proposed hostname.
    """
    ok = bool(re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", hostname)) and len(hostname) <= 63
    return {"hostname": hostname, "valid": ok}


def _get_ami():
    ssm = boto3.client("ssm", region_name=REGION)
    return ssm.get_parameter(
        Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
    )["Parameter"]["Value"]


@tool
def provision_ec2(hostname: str, instance_count: int, tags: dict) -> dict:
    """Provision real EC2 instances via the task IAM role.

    Args:
        hostname: The Name tag / hostname for the instances.
        instance_count: How many instances (capped at 3).
        tags: Additional tags to apply.
    """
    ec2 = boto3.client("ec2", region_name=REGION)
    count = min(int(instance_count), 3)
    tag_spec = [
        {"Key": "Name", "Value": hostname},
        {"Key": "ProvisionedBy", "Value": "infrastructure-agent"},
    ]
    for k, v in (tags or {}).items():
        if k != "Name":
            tag_spec.append({"Key": str(k)[:127], "Value": str(v)[:255]})
    resp = ec2.run_instances(
        ImageId=_get_ami(), InstanceType="t3.micro",
        MinCount=count, MaxCount=count,
        SubnetId=SUBNET_ID, SecurityGroupIds=[SG_ID],
        BlockDeviceMappings=[{"DeviceName": "/dev/xvda",
                              "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "Encrypted": True}}],
        TagSpecifications=[{"ResourceType": "instance", "Tags": tag_spec}],
    )
    ids = [i["InstanceId"] for i in resp["Instances"]]
    return {"instance_ids": ids, "count": count, "status": "provisioning"}


REQ_PROMPT = """You are a Requirement Gathering Agent for infrastructure provisioning.
Collect all info needed via natural conversation. If given a ticket ID, look it up.
Ask targeted follow-ups for missing fields, validate the hostname and plan, then
present a confirmed JSON payload. Required: environment, instance_type,
operating_system, project_name, owner, cost_center, hostname."""

PROV_PROMPT = """You are a Provisioning Agent. Validate the requirements with
validate_plan, then provision the instances with provision_ec2 and report the result
including the real instance IDs."""

_req_agent = None
_prov_agent = None


def _req():
    global _req_agent
    if _req_agent is None:
        _req_agent = Agent(model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
                           system_prompt=REQ_PROMPT, tools=[lookup_ticket, validate_plan, validate_hostname])
    return _req_agent


def _prov():
    global _prov_agent
    if _prov_agent is None:
        _prov_agent = Agent(model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
                            system_prompt=PROV_PROMPT, tools=[validate_plan, validate_hostname, provision_ec2])
    return _prov_agent


# ─── API ────────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str
    ticket_id: str | None = None


class ProvisionRequest(BaseModel):
    session_id: str | None = None
    requirements: dict


class CreateTicketRequest(BaseModel):
    summary: str
    description: str = ""
    priority: str = "Medium"
    assignee: str = "Unassigned"
    environment: str = ""
    project_type: str = ""
    requested_resources: str = ""


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/api/tickets")
def list_tickets():
    return list(TICKETS.values())


@app.post("/api/tickets")
def create_ticket(req: CreateTicketRequest):
    global TICKET_COUNTER
    TICKET_COUNTER += 1
    key = f"INFRA-{TICKET_COUNTER}"
    TICKETS[key] = {
        "ticket_id": key, "summary": req.summary, "status": "Open",
        "priority": req.priority, "assignee": req.assignee, "project": "Infrastructure",
        "description": req.description,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S.000+0000", time.gmtime()),
        "environment": req.environment, "project_type": req.project_type,
        "requested_resources": req.requested_resources,
    }
    return {"key": key}


@app.post("/api/chat")
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    msg = req.message
    if req.ticket_id and not req.session_id:
        msg = f"Load ticket {req.ticket_id} and help me gather the remaining requirements. {msg}"
    response = _req()(msg)
    text = str(response)
    return {"session_id": session_id, "response": text, "turn_count": 1,
            "stage": "gathering", "handoff_ready": "HANDOFF_READY" in text}


@app.post("/api/provision")
def provision(req: ProvisionRequest):
    session_id = req.session_id or str(uuid.uuid4())
    prompt = f"Execute this provisioning plan:\n```json\n{json.dumps(req.requirements, indent=2)}\n```"
    response = _prov()(prompt)
    return {"session_id": session_id, "response": str(response),
            "ticket_id": req.requirements.get("ticket_id"), "stage": "executed"}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


# UI is injected at container build via the INDEX_HTML placeholder below.
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Infrastructure Provisioning Portal</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {
    --bg: #f4f5f7;
    --surface: #ffffff;
    --border: #e2e4e8;
    --text: #1a1d21;
    --text-muted: #6b7280;
    --accent: #232f3e;
    --accent-2: #ff9900;
    --green: #067d62;
    --red: #b91c1c;
    --blue: #0972d3;
    --radius: 10px;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column;
  }
  header {
    background: var(--accent); color: #fff; padding: 14px 24px;
    display: flex; align-items: center; gap: 12px; flex-shrink: 0;
  }
  header .logo { background: var(--accent-2); color: var(--accent); font-weight: 800; border-radius: 6px; padding: 4px 8px; font-size: 14px; }
  header h1 { font-size: 17px; font-weight: 600; }
  header .sub { font-size: 12px; opacity: .7; margin-left: auto; }

  main { flex: 1; overflow: hidden; display: flex; flex-direction: column; }

  /* ---------- Dashboard ---------- */
  #dashboard { padding: 24px; overflow-y: auto; }
  .metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
  .metric-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 18px 20px;
  }
  .metric-card .label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .05em; }
  .metric-card .value { font-size: 32px; font-weight: 700; margin-top: 4px; }
  .section-title { font-size: 15px; font-weight: 600; margin: 8px 0 12px; }
  .ticket-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
  .ticket-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 18px; cursor: pointer; transition: box-shadow .15s, transform .15s;
  }
  .ticket-card:hover { box-shadow: 0 4px 16px rgba(0,0,0,.08); transform: translateY(-2px); }
  .ticket-card .key { font-weight: 700; color: var(--blue); font-size: 14px; }
  .ticket-card .summary { font-size: 14px; margin: 8px 0 12px; line-height: 1.4; }
  .ticket-card .meta { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .badge { font-size: 11px; padding: 3px 9px; border-radius: 20px; font-weight: 600; }
  .badge.status-Open { background: #e0f2fe; color: #075985; }
  .badge.status-Analysis { background: #fef3c7; color: #92400e; }
  .badge.status-ReadyforProd { background: #d1fae5; color: #065f46; }
  .badge.priority-Critical { background: #fee2e2; color: var(--red); }
  .badge.priority-High { background: #ffedd5; color: #c2410c; }
  .badge.priority-Medium { background: #f3f4f6; color: #4b5563; }
  .ticket-card .assignee { font-size: 12px; color: var(--text-muted); margin-left: auto; }

  /* ---------- Workspace (split panel) ---------- */
  #workspace { display: none; flex: 1; overflow: hidden; }
  #workspace.active { display: flex; }
  .chat-pane { flex: 1.4; display: flex; flex-direction: column; border-right: 1px solid var(--border); background: var(--surface); }
  .chat-header {
    padding: 12px 20px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px;
  }
  .chat-header button.back {
    background: none; border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px;
    cursor: pointer; font-size: 13px; color: var(--text);
  }
  .chat-header .title { font-weight: 600; font-size: 14px; }
  .chat-header .stage-pill {
    margin-left: auto; font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 20px;
    background: #e0f2fe; color: #075985;
  }
  #messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 14px; }
  .msg { max-width: 85%; padding: 12px 16px; border-radius: 12px; font-size: 14px; line-height: 1.55; }
  .msg.user { align-self: flex-end; background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
  .msg.agent { align-self: flex-start; background: #f3f4f6; border-bottom-left-radius: 4px; }
  .msg.agent table { border-collapse: collapse; margin: 8px 0; font-size: 13px; }
  .msg.agent th, .msg.agent td { border: 1px solid #d1d5db; padding: 5px 10px; text-align: left; }
  .msg.agent th { background: #e5e7eb; }
  .msg.agent pre { background: #1e293b; color: #e2e8f0; padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 12px; margin: 8px 0; }
  .msg.agent code { font-size: 12.5px; }
  .msg.agent p { margin: 6px 0; }
  .msg.agent h2, .msg.agent h3, .msg.agent h4 { margin: 10px 0 6px; }
  .msg.agent ul, .msg.agent ol { margin: 6px 0 6px 20px; }
  .msg.typing { color: var(--text-muted); font-style: italic; background: none; }
  .chat-input { display: flex; gap: 10px; padding: 14px 20px; border-top: 1px solid var(--border); }
  .chat-input textarea {
    flex: 1; resize: none; border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px;
    font-size: 14px; font-family: inherit; height: 44px; outline: none;
  }
  .chat-input textarea:focus { border-color: var(--blue); }
  .chat-input button {
    background: var(--accent-2); color: var(--accent); border: none; border-radius: 8px;
    padding: 0 22px; font-weight: 700; font-size: 14px; cursor: pointer;
  }
  .chat-input button:disabled { opacity: .5; cursor: not-allowed; }

  /* ---------- Right panel ---------- */
  .detail-pane { flex: 1; display: flex; flex-direction: column; background: var(--surface); overflow: hidden; }
  .tabs { display: flex; border-bottom: 1px solid var(--border); flex-shrink: 0; }
  .tabs button {
    flex: 1; padding: 12px; background: none; border: none; border-bottom: 2px solid transparent;
    font-size: 13px; font-weight: 600; color: var(--text-muted); cursor: pointer;
  }
  .tabs button.active { color: var(--blue); border-bottom-color: var(--blue); }
  .tab-content { flex: 1; overflow-y: auto; padding: 20px; display: none; }
  .tab-content.active { display: block; }
  .field-row { display: flex; justify-content: space-between; padding: 9px 0; border-bottom: 1px solid #f0f1f3; font-size: 13px; }
  .field-row .k { color: var(--text-muted); }
  .field-row .v { font-weight: 500; text-align: right; max-width: 60%; }
  #plan-json {
    width: 100%; height: 300px; font-family: ui-monospace, monospace; font-size: 12px;
    border: 1px solid var(--border); border-radius: 8px; padding: 12px; resize: vertical;
  }
  .exec-btn {
    width: 100%; margin-top: 12px; background: var(--green); color: #fff; border: none;
    border-radius: 8px; padding: 12px; font-size: 14px; font-weight: 700; cursor: pointer;
  }
  .exec-btn:disabled { opacity: .5; cursor: not-allowed; }
  #exec-result { margin-top: 16px; font-size: 13px; line-height: 1.55; }
  #exec-result table { border-collapse: collapse; margin: 8px 0; font-size: 12.5px; width: 100%; }
  #exec-result th, #exec-result td { border: 1px solid #d1d5db; padding: 5px 8px; text-align: left; }
  #exec-result th { background: #e5e7eb; }
  #exec-result h2, #exec-result h3 { margin: 10px 0 6px; font-size: 15px; }
  .hint { font-size: 12px; color: var(--text-muted); margin-bottom: 8px; }
  .spinner {
    display: inline-block; width: 14px; height: 14px; border: 2px solid #d1d5db;
    border-top-color: var(--blue); border-radius: 50%; animation: spin .8s linear infinite;
    vertical-align: middle; margin-right: 6px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<header>
  <span class="logo">EPX</span>
  <h1>Infrastructure Provisioning Portal</h1>
  <span class="sub">Powered by Amazon Bedrock AgentCore + Strands Agents SDK</span>
</header>

<main>
  <!-- Dashboard view -->
  <div id="dashboard">
    <div class="metrics">
      <div class="metric-card"><div class="label">Total Tickets</div><div class="value" id="m-total">–</div></div>
      <div class="metric-card"><div class="label">Open</div><div class="value" id="m-open">–</div></div>
      <div class="metric-card"><div class="label">In Analysis</div><div class="value" id="m-analysis">–</div></div>
      <div class="metric-card"><div class="label">Ready for Prod</div><div class="value" id="m-ready">–</div></div>
    </div>
    <div style="display:flex; align-items:center; gap:14px; margin-bottom: 12px;">
      <div class="section-title" style="margin:0">Provisioning Requests (JIRA-synced)</div>
      <button onclick="showCreateForm()" style="background:var(--green);color:#fff;border:none;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;">+ Create Ticket</button>
    </div>

    <!-- Create ticket form (hidden by default) -->
    <div id="create-form" style="display:none; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:20px; margin-bottom:20px;">
      <h3 style="font-size:15px; margin-bottom:14px;">New Provisioning Request</h3>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
        <div>
          <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px;">Summary *</label>
          <input id="cf-summary" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;" placeholder="e.g., Provision EC2 for data pipeline">
        </div>
        <div>
          <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px;">Priority</label>
          <select id="cf-priority" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;">
            <option>Medium</option><option>High</option><option>Critical</option><option>Low</option>
          </select>
        </div>
        <div style="grid-column:1/-1;">
          <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px;">Description</label>
          <textarea id="cf-description" rows="3" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;font-family:inherit;resize:vertical;" placeholder="Describe what you need provisioned, how many instances, sizing, environment, etc."></textarea>
        </div>
        <div>
          <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px;">Environment</label>
          <select id="cf-env" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;">
            <option value="development">Development</option><option value="staging">Staging</option><option value="production">Production</option>
          </select>
        </div>
        <div>
          <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px;">Assignee</label>
          <input id="cf-assignee" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;" placeholder="Your name" value="Jane Doe">
        </div>
        <div>
          <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px;">Requested Resources</label>
          <input id="cf-resources" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;" placeholder="e.g., EC2 Linux x2, t3.micro">
        </div>
        <div>
          <label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px;">Project Type</label>
          <input id="cf-projtype" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;" placeholder="e.g., Standard Build, Database Build">
        </div>
      </div>
      <div style="margin-top:16px; display:flex; gap:10px;">
        <button onclick="submitTicket()" style="background:var(--green);color:#fff;border:none;border-radius:8px;padding:10px 20px;font-size:13px;font-weight:600;cursor:pointer;">Create & Open</button>
        <button onclick="document.getElementById('create-form').style.display='none'" style="background:none;border:1px solid var(--border);border-radius:8px;padding:10px 20px;font-size:13px;cursor:pointer;">Cancel</button>
        <span id="cf-status" style="font-size:12px;color:var(--text-muted);align-self:center;"></span>
      </div>
    </div>

    <div class="ticket-grid" id="ticket-grid"><div class="hint"><span class="spinner"></span>Loading tickets…</div></div>
  </div>

  <!-- Split-panel workspace -->
  <div id="workspace">
    <div class="chat-pane">
      <div class="chat-header">
        <button class="back" onclick="showDashboard()">← Tickets</button>
        <span class="title" id="ws-title">–</span>
        <span class="stage-pill" id="ws-stage">gathering</span>
      </div>
      <div id="messages"></div>
      <div class="chat-input">
        <textarea id="input" placeholder="Describe your requirements or answer the agent…"
          onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMessage();}"></textarea>
        <button id="send-btn" onclick="sendMessage()">Send</button>
      </div>
    </div>

    <div class="detail-pane">
      <div class="tabs">
        <button class="active" onclick="showTab('ticket', this)">Ticket Details</button>
        <button onclick="showTab('plan', this)">Requirements Plan</button>
        <button onclick="showTab('exec', this)">Execution</button>
      </div>
      <div class="tab-content active" id="tab-ticket"></div>
      <div class="tab-content" id="tab-plan">
        <div class="hint">The JSON payload below is pre-filled from the ticket. Edit as needed, or paste the payload the agent produced in chat, then execute.</div>
        <textarea id="plan-json"></textarea>
        <button class="exec-btn" id="exec-btn" onclick="executePlan()">▶ Execute Provisioning Plan</button>
      </div>
      <div class="tab-content" id="tab-exec">
        <div class="hint">Execution output from the Provisioning Agent appears here.</div>
        <div id="exec-result"><em>No execution yet.</em></div>
      </div>
    </div>
  </div>
</main>

<script>
let sessionId = null;
let currentTicket = null;

async function loadTickets() {
  const grid = document.getElementById('ticket-grid');
  try {
    const res = await fetch('/api/tickets');
    const tickets = await res.json();
    const counts = { total: tickets.length, Open: 0, Analysis: 0, 'Ready for Prod': 0 };
    grid.innerHTML = '';
    tickets.forEach(t => {
      counts[t.status] = (counts[t.status] || 0) + 1;
      const card = document.createElement('div');
      card.className = 'ticket-card';
      card.onclick = () => openTicket(t);
      card.innerHTML = `
        <div class="key">${t.ticket_id}</div>
        <div class="summary">${t.summary}</div>
        <div class="meta">
          <span class="badge status-${t.status.replaceAll(' ','')}">${t.status}</span>
          <span class="badge priority-${t.priority}">${t.priority}</span>
          <span class="assignee">${t.assignee}</span>
        </div>`;
      grid.appendChild(card);
    });
    document.getElementById('m-total').textContent = counts.total;
    document.getElementById('m-open').textContent = counts.Open || 0;
    document.getElementById('m-analysis').textContent = counts.Analysis || 0;
    document.getElementById('m-ready').textContent = counts['Ready for Prod'] || 0;
  } catch (e) {
    grid.innerHTML = `<div class="hint">Failed to load tickets: ${e}</div>`;
  }
}

function openTicket(t) {
  currentTicket = t;
  sessionId = null;
  document.getElementById('dashboard').style.display = 'none';
  document.getElementById('workspace').classList.add('active');
  document.getElementById('ws-title').textContent = `${t.ticket_id} — ${t.summary}`;
  document.getElementById('ws-stage').textContent = 'gathering';
  document.getElementById('messages').innerHTML = '';

  // Ticket details tab
  const rows = [
    ['Ticket', t.ticket_id], ['Summary', t.summary], ['Status', t.status],
    ['Priority', t.priority], ['Assignee', t.assignee], ['Project', t.project],
    ['Environment', t.environment || '—'], ['Request Type', t.project_type || '—'],
    ['Resources', t.requested_resources || '—'], ['Created', (t.created||'').split('T')[0]],
  ];
  document.getElementById('tab-ticket').innerHTML = rows.map(([k, v]) =>
    `<div class="field-row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('') +
    `<div class="hint" style="margin-top:14px">${t.description || ''}</div>`;

  // Pre-fill the requirements plan template
  document.getElementById('plan-json').value = JSON.stringify({
    ticket_id: t.ticket_id,
    environment: t.environment || 'development',
    instance_type: 'm5.large',
    operating_system: 'Ubuntu 22.04',
    vpc: 'vpc-core-dev',
    subnet: 'subnet-private-a',
    security_group: 'app-server',
    storage_size_gb: 100,
    project_name: 'analytics',
    owner: 'owner@example.com',
    cost_center: 'CC-0000',
    hostname: 'dev-analytics-app-001',
    encryption_enabled: true,
    backup_policy: 'daily',
    request_type: 'ec2_linux',
    instance_count: 1,
    tags: { project: 'analytics', environment: t.environment || 'development',
            owner: 'owner@example.com', 'cost-center': 'CC-0000' }
  }, null, 2);
  document.getElementById('exec-result').innerHTML = '<em>No execution yet.</em>';

  // Kick off the conversation with the ticket loaded
  addMsg('user', `Load ticket ${t.ticket_id} and help me gather the remaining requirements.`);
  callChat(`I have ticket ${t.ticket_id} ready for provisioning. Please look it up and help me gather the remaining requirements.`, t.ticket_id);
}

function showDashboard() {
  document.getElementById('workspace').classList.remove('active');
  document.getElementById('dashboard').style.display = 'block';
  loadTickets();
}

function showTab(name, btn) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tabs button').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
}

function addMsg(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  if (role === 'agent') div.innerHTML = marked.parse(text);
  else div.textContent = text;
  document.getElementById('messages').appendChild(div);
  div.scrollIntoView({ behavior: 'smooth' });
  return div;
}

async function callChat(message, ticketId) {
  const btn = document.getElementById('send-btn');
  btn.disabled = true;
  const typing = addMsg('agent typing', '');
  typing.innerHTML = '<span class="spinner"></span>Agent is thinking…';
  typing.classList.add('typing');
  try {
    const body = { message };
    if (sessionId) body.session_id = sessionId;
    if (ticketId) body.ticket_id = ticketId;
    const res = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    sessionId = data.session_id;
    typing.remove();
    addMsg('agent', data.response);
    document.getElementById('ws-stage').textContent =
      `${data.stage} · turn ${data.turn_count}` + (data.handoff_ready ? ' · HANDOFF READY' : '');
  } catch (e) {
    typing.remove();
    addMsg('agent', `⚠️ Error: ${e}`);
  } finally {
    btn.disabled = false;
  }
}

function sendMessage() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  addMsg('user', text);
  callChat(text);
}

async function executePlan() {
  const btn = document.getElementById('exec-btn');
  const result = document.getElementById('exec-result');
  let requirements;
  try {
    requirements = JSON.parse(document.getElementById('plan-json').value);
  } catch (e) {
    result.innerHTML = `<span style="color:var(--red)">Invalid JSON: ${e.message}</span>`;
    showTab('exec', document.querySelectorAll('.tabs button')[2]);
    return;
  }
  btn.disabled = true;
  showTab('exec', document.querySelectorAll('.tabs button')[2]);
  result.innerHTML = '<span class="spinner"></span>Provisioning Agent is validating and executing… (may take 1–2 minutes; it polls the AWX job to completion)';
  try {
    const res = await fetch('/api/provision', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requirements })
    });
    const data = await res.json();
    if (data.detail) {
      result.innerHTML = `<span style="color:var(--red)">${data.detail}</span>`;
    } else {
      result.innerHTML = marked.parse(data.response);
    }
  } catch (e) {
    result.innerHTML = `<span style="color:var(--red)">Error: ${e}</span>`;
  } finally {
    btn.disabled = false;
  }
}

function showCreateForm() {
  document.getElementById('create-form').style.display = 'block';
  document.getElementById('cf-summary').focus();
}

async function submitTicket() {
  const status = document.getElementById('cf-status');
  const summary = document.getElementById('cf-summary').value.trim();
  if (!summary) { status.textContent = 'Summary is required.'; status.style.color='var(--red)'; return; }
  status.textContent = 'Creating...'; status.style.color = 'var(--text-muted)';

  try {
    const res = await fetch('/api/tickets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        summary,
        description: document.getElementById('cf-description').value.trim(),
        priority: document.getElementById('cf-priority').value,
        assignee: document.getElementById('cf-assignee').value.trim() || 'Unassigned',
        environment: document.getElementById('cf-env').value,
        project_type: document.getElementById('cf-projtype').value.trim(),
        requested_resources: document.getElementById('cf-resources').value.trim(),
      })
    });
    const data = await res.json();
    if (!res.ok) { status.textContent = data.detail || 'Failed'; status.style.color='var(--red)'; return; }
    document.getElementById('create-form').style.display = 'none';
    // Reload and open the new ticket
    await loadTickets();
    const ticketsRes = await fetch('/api/tickets');
    const tickets = await ticketsRes.json();
    const newT = tickets.find(t => t.ticket_id === data.key);
    if (newT) openTicket(newT);
  } catch (e) {
    status.textContent = `Error: ${e}`; status.style.color='var(--red)';
  }
}

// Deep-link support: /?ticket=INFRA-1234 auto-opens the workspace for that ticket
async function init() {
  await loadTickets();
  const params = new URLSearchParams(location.search);
  const ticketKey = params.get('ticket');
  if (ticketKey) {
    const res = await fetch('/api/tickets');
    const tickets = await res.json();
    const t = tickets.find(x => x.ticket_id === ticketKey);
    if (t) openTicket(t);
  }
}
init();
</script>
</body>
</html>
"""
