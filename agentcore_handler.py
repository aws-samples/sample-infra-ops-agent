"""Amazon Bedrock AgentCore Runtime handler — Infrastructure Provisioning Agent.

Self-contained entrypoint that runs the Strands agent inside AgentCore Runtime.
All tool definitions are inline to avoid import-path issues in the container.
"""

import json
import logging
import os
import sys
import re
import time

import requests
import yaml
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_USER_EMAIL = os.getenv("JIRA_USER_EMAIL", "")
AWX_BASE_URL = os.getenv("AWX_BASE_URL", "")
AWX_API_TOKEN = os.getenv("AWX_API_TOKEN", "")

# Inline taxonomy (subset for the container — full version in config/taxonomy.yaml)
TAXONOMY = {
    "compute": {
        "instance_types": ["t3.micro", "t3.small", "t3.medium", "t3.large", "m5.large", "m5.xlarge"],
        "operating_systems": ["Amazon Linux 2023", "Ubuntu 22.04", "Ubuntu 24.04", "Windows Server 2022"],
        "environments": ["development", "staging", "production"],
    },
    "networking": {
        "vpcs": ["vpc-core-dev", "vpc-core-staging", "vpc-core-prod"],
    },
    "naming": {"pattern": "{env}-{project}-{role}-{seq:03d}", "max_length": 63},
    "compliance": {
        "required_tags": ["project", "environment", "owner", "cost-center"],
        "encryption_at_rest_required": True,
    },
}


# ─── Tools ──────────────────────────────────────────────────────────────────

@tool
def lookup_ticket(ticket_id: str) -> dict:
    """Look up a JIRA provisioning ticket by ID and return its metadata.

    Args:
        ticket_id: The JIRA ticket ID (e.g., INFRA-1234).
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_id}"
    resp = requests.get(url, headers={"Content-Type": "application/json"}, timeout=15)
    resp.raise_for_status()
    issue = resp.json()
    fields = issue["fields"]
    return {
        "ticket_id": issue["key"],
        "summary": fields.get("summary", ""),
        "status": fields.get("status", {}).get("name", "Unknown"),
        "priority": fields.get("priority", {}).get("name", "Medium"),
        "assignee": fields.get("assignee", {}).get("displayName", "Unassigned"),
        "description": fields.get("description", ""),
        "environment": fields.get("customfield_10001", ""),
        "requested_resources": fields.get("customfield_10003", ""),
    }


@tool
def validate_plan(plan: dict) -> dict:
    """Validate a provisioning plan against organizational policies.

    Args:
        plan: The provisioning plan with instance_type, environment, os, vpc, tags, etc.
    """
    violations = []
    if plan.get("instance_type") and plan["instance_type"] not in TAXONOMY["compute"]["instance_types"]:
        violations.append(f"Instance type '{plan['instance_type']}' not approved.")
    if plan.get("operating_system") and plan["operating_system"] not in TAXONOMY["compute"]["operating_systems"]:
        violations.append(f"OS '{plan['operating_system']}' not approved.")
    if plan.get("environment") and plan["environment"] not in TAXONOMY["compute"]["environments"]:
        violations.append(f"Environment '{plan['environment']}' not valid.")
    tags = plan.get("tags", {})
    missing_tags = [t for t in TAXONOMY["compliance"]["required_tags"] if t not in tags]
    if missing_tags:
        violations.append(f"Missing required tags: {missing_tags}")
    if TAXONOMY["compliance"]["encryption_at_rest_required"]:
        if not (plan.get("encryption_enabled") or plan.get("encryption_at_rest")):
            violations.append("Encryption at rest is required.")
    return {"valid": len(violations) == 0, "violations": violations}


@tool
def validate_hostname(hostname: str, environment: str, project: str, role: str) -> dict:
    """Validate a hostname against the naming convention.

    Args:
        hostname: The proposed hostname.
        environment: Target environment.
        project: Project identifier.
        role: Server role.
    """
    violations = []
    max_len = TAXONOMY["naming"]["max_length"]
    if len(hostname) > max_len:
        violations.append(f"Exceeds {max_len} char limit.")
    if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", hostname):
        violations.append("Must be lowercase alphanumeric with hyphens only.")
    return {"hostname": hostname, "valid": len(violations) == 0, "violations": violations}


@tool
def invoke_awx_template(template_name: str, extra_vars: dict) -> dict:
    """Launch an AWX job template to execute IaC pipelines.

    Args:
        template_name: The AWX template name (e.g., "Create Linux EC2 Instance").
        extra_vars: Variables to pass to the playbook.
    """
    search = requests.get(
        f"{AWX_BASE_URL}/api/v2/job_templates/",
        params={"name": template_name},
        headers={"Authorization": f"Bearer {AWX_API_TOKEN}"},
        timeout=15,
    )
    search.raise_for_status()
    results = search.json().get("results", [])
    if not results:
        return {"error": f"Template '{template_name}' not found."}
    template_id = results[0]["id"]
    launch = requests.post(
        f"{AWX_BASE_URL}/api/v2/job_templates/{template_id}/launch/",
        json={"extra_vars": extra_vars},
        headers={"Authorization": f"Bearer {AWX_API_TOKEN}", "Content-Type": "application/json"},
        timeout=30,
    )
    launch.raise_for_status()
    job = launch.json()
    return {"job_id": job["id"], "template_name": template_name, "status": job.get("status", "pending")}


@tool
def get_build_status(job_id: int) -> dict:
    """Check an AWX job's current status.

    Args:
        job_id: The AWX job ID.
    """
    resp = requests.get(
        f"{AWX_BASE_URL}/api/v2/jobs/{job_id}/",
        headers={"Authorization": f"Bearer {AWX_API_TOKEN}"},
        timeout=15,
    )
    resp.raise_for_status()
    job = resp.json()
    return {"job_id": job["id"], "status": job.get("status"), "elapsed": job.get("elapsed", 0)}


# ─── Agent definitions ──────────────────────────────────────────────────────

REQUIREMENT_PROMPT = """\
You are a Requirement Gathering Agent for infrastructure provisioning. Collect all
information needed to provision cloud infrastructure via natural language conversation.

Workflow:
1. If given a JIRA ticket ID, look it up with the lookup_ticket tool.
2. Ask targeted follow-up questions for any missing fields.
3. Validate the hostname and full plan against policies.
4. Present a confirmed JSON payload when ready.

Required fields: environment, instance_type, operating_system, vpc, subnet,
security_group, storage_size_gb, project_name, owner, cost_center, hostname.
"""

PROVISIONING_PROMPT = """\
You are a Provisioning Agent. You receive a validated requirements payload and
execute the IaC pipeline.

Workflow:
1. Validate the plan with validate_plan.
2. If valid, invoke the AWX template and monitor completion.
3. Report the outcome.
"""


def _create_requirement_agent() -> Agent:
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    return Agent(model=model, system_prompt=REQUIREMENT_PROMPT,
                 tools=[lookup_ticket, validate_plan, validate_hostname])


def _create_provisioning_agent() -> Agent:
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    return Agent(model=model, system_prompt=PROVISIONING_PROMPT,
                 tools=[invoke_awx_template, get_build_status, validate_plan, validate_hostname])


_req_agent = None
_prov_agent = None


def handler(event: dict, context=None) -> dict:
    """Route between agents based on the event payload."""
    global _req_agent, _prov_agent

    agent_type = event.get("agent_type", "requirement")
    user_message = event.get("user_message", "")
    requirements = event.get("requirements")
    ticket_id = event.get("ticket_id")

    if agent_type == "provisioning" or requirements:
        if _prov_agent is None:
            _prov_agent = _create_provisioning_agent()
        if requirements:
            prompt = f"Execute this plan:\n```json\n{json.dumps(requirements, indent=2)}\n```"
        else:
            prompt = user_message
        response = _prov_agent(prompt)
        return {"response": str(response), "agent_type": "provisioning"}
    else:
        if _req_agent is None:
            _req_agent = _create_requirement_agent()
        if ticket_id and not user_message:
            user_message = f"Load ticket {ticket_id} and help me gather requirements."
        response = _req_agent(user_message)
        return {"response": str(response), "agent_type": "requirement"}


# ─── AgentCore HTTP runtime entry ───────────────────────────────────────────

if __name__ == "__main__":
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def handle_request(event: dict, context=None) -> dict:
        return handler(event, context)

    app.run()
