"""Amazon Bedrock AgentCore Runtime entrypoint for the Provisioning Agent.

This module exposes the Provisioning Agent as an AgentCore-compatible runtime.
It validates the requirements payload, executes the appropriate AWX template,
and reports outcomes back through the JIRA integration.

Deploy with:
    agentcore configure --agent-name provisioning
    agentcore launch
"""

import json
import logging

from strands import Agent
from strands.models.bedrock import BedrockModel

from agents.provisioning_agent import SYSTEM_PROMPT, build_execution_prompt
from config import AWS_REGION, BEDROCK_MODEL_ID
from tools.awx_tools import invoke_awx_template, get_build_status, wait_for_job_completion
from tools.jira_tools import update_ticket_status
from tools.validation_tools import validate_plan_against_policy, validate_naming

logger = logging.getLogger(__name__)


def create_agent() -> Agent:
    """Initialize the Provisioning Agent for the AgentCore runtime."""
    model = BedrockModel(
        model_id=BEDROCK_MODEL_ID,
        region_name=AWS_REGION,
    )

    return Agent(
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


_agent: Agent | None = None


def get_agent() -> Agent:
    """Return the singleton agent instance."""
    global _agent
    if _agent is None:
        _agent = create_agent()
    return _agent


def handler(event: dict, context: dict) -> dict:
    """AgentCore runtime handler for provisioning execution.

    Receives an event containing the validated requirements payload from the
    Requirement Gathering Agent's handoff, runs the Provisioning Agent to
    execute the IaC pipeline, and returns the build outcome.

    Args:
        event: The incoming event with fields:
            - session_id: The active session identifier
            - requirements: The validated provisioning requirements (dict)
            - user_message: (optional) Direct message for interactive mode
        context: Runtime context (AgentCore metadata, identity info)

    Returns:
        A dictionary with the execution outcome, job IDs, and updated ticket
        information.
    """
    session_id = event.get("session_id", "unknown")
    requirements = event.get("requirements")
    user_message = event.get("user_message")

    logger.info("Processing provisioning for session %s", session_id)

    agent = get_agent()

    if requirements:
        prompt = build_execution_prompt(requirements)
    elif user_message:
        prompt = user_message
    else:
        return {
            "session_id": session_id,
            "error": "No requirements payload or user message provided.",
        }

    response = agent(prompt)
    assistant_message = str(response)

    return {
        "session_id": session_id,
        "response": assistant_message,
        "ticket_id": requirements.get("ticket_id") if requirements else None,
        "stage": "executed",
    }
