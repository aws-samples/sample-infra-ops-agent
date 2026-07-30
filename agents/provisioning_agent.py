"""Provisioning Agent — plan validation and IaC execution.

This agent receives a structured JSON requirements payload from the Requirement
Gathering Agent, performs a final policy validation, then invokes the AWX
templates mapped to each provisioning action.

Usage (standalone/development):
    python -m agents.provisioning_agent --requirements-file /tmp/requirements.json

In production this agent runs inside an Amazon Bedrock AgentCore Runtime
container (see runtime/provisioning_runtime.py).
"""

import argparse
import json
import sys
from pathlib import Path

from strands import Agent
from strands.models.bedrock import BedrockModel

from config import (
    AWS_REGION,
    BEDROCK_MODEL_ID,
    BEDROCK_GUARDRAIL_ID,
    BEDROCK_GUARDRAIL_VERSION,
)
from tools.awx_tools import invoke_awx_template, get_build_status, wait_for_job_completion
from tools.jira_tools import update_ticket_status
from tools.validation_tools import validate_plan_against_policy, validate_naming

SYSTEM_PROMPT = """\
You are a Provisioning Agent for infrastructure builds. You receive a validated
JSON requirements payload from the Requirement Gathering Agent and execute the
provisioning pipeline.

Your workflow:
1. Review the requirements payload for completeness.
2. Run a final validation against organizational policies (naming, compliance,
   networking, storage constraints).
3. If validation passes, identify the correct AWX job template for the request
   type and invoke it with the appropriate extra_vars.
4. Monitor the job until completion and report the result.
5. Update the JIRA ticket with the build outcome and generate a build
   requirement sheet.

Rules:
- Never execute a plan that fails policy validation. Report the violations and
  wait for corrections.
- Provisioning requires human approval. invoke_awx_template will refuse to run
  without a valid approval token, so present the validated plan to the operator
  and only execute once approval is granted. Do not attempt to bypass this.
- After triggering a build, poll for completion and report the final status.
- Always update the JIRA ticket with outcomes (success or failure).
- If execution fails, capture the error details and update the ticket
  accordingly.
"""

# Mapping from request types to AWX template names
TEMPLATE_MAPPING = {
    "ec2_linux": "Create Linux EC2 Instance",
    "ec2_windows": "Create Windows EC2 Instance",
    "precheck": "PreCheck Assessment",
    "os_patch": "OS Patch Management",
}


def create_agent() -> Agent:
    """Create and return the Provisioning Agent."""
    # H3: attach the Bedrock Guardrail (prompt-injection / denied-topics / PII)
    # to every model invocation when one is configured.
    model_kwargs = {"model_id": BEDROCK_MODEL_ID, "region_name": AWS_REGION}
    if BEDROCK_GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = BEDROCK_GUARDRAIL_ID
        model_kwargs["guardrail_version"] = BEDROCK_GUARDRAIL_VERSION
    model = BedrockModel(**model_kwargs)

    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            invoke_awx_template,
            get_build_status,
            wait_for_job_completion,
            update_ticket_status,
            validate_plan_against_policy,
            validate_naming,
        ],
    )
    return agent


def build_execution_prompt(requirements: dict) -> str:
    """Build the initial prompt for the Provisioning Agent from requirements."""
    req_json = json.dumps(requirements, indent=2)
    return (
        f"I have the following validated provisioning requirements to execute:\n\n"
        f"```json\n{req_json}\n```\n\n"
        f"Please validate this plan against our organizational policies, then "
        f"execute the appropriate AWX template. Update the JIRA ticket "
        f"({requirements.get('ticket_id', 'unknown')}) with the outcome."
    )


def run_from_file(requirements_path: str):
    """Execute provisioning from a JSON requirements file."""
    path = Path(requirements_path)
    if not path.exists():
        print(f"Error: Requirements file not found: {requirements_path}")
        sys.exit(1)

    with open(path) as f:
        requirements = json.load(f)

    agent = create_agent()
    prompt = build_execution_prompt(requirements)

    print("=" * 60)
    print("Infrastructure Provisioning Agent")
    print(f"Processing: {requirements.get('ticket_id', 'unknown')}")
    print("=" * 60)

    response = agent(prompt)
    print(f"\nAgent: {response}")


def run_interactive():
    """Run the agent in interactive mode for development/testing."""
    agent = create_agent()

    print("=" * 60)
    print("Infrastructure Provisioning Agent (Interactive)")
    print("Paste a JSON requirements payload or describe what to provision.")
    print("Type 'quit' to exit.")
    print("=" * 60)

    while True:
        user_input = input("\nYou: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            break

        response = agent(user_input)
        print(f"\nAgent: {response}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Provisioning Agent")
    parser.add_argument(
        "--requirements-file",
        type=str,
        help="Path to a JSON file with provisioning requirements",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run in interactive mode",
    )
    args = parser.parse_args()

    if args.requirements_file:
        run_from_file(args.requirements_file)
    else:
        run_interactive()
