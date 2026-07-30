"""Amazon Bedrock AgentCore Runtime entrypoint for the Requirement Gathering Agent.

This module exposes the agent as an AgentCore-compatible runtime that handles
incoming requests via the AgentCore orchestration layer. It configures the
agent with its tool set, session persistence, and IAM-scoped identity.

Deploy with:
    agentcore configure --agent-name requirement-gathering
    agentcore launch
"""

import json
import logging

from strands import Agent
from strands.models.bedrock import BedrockModel

from agents.requirement_gathering_agent import SYSTEM_PROMPT
from config import AWS_REGION, BEDROCK_MODEL_ID
from session.dynamodb_session import SessionStore
from tools.jira_tools import lookup_ticket, get_ticket_requirements
from tools.validation_tools import validate_naming, validate_plan_against_policy

logger = logging.getLogger(__name__)

# Session store shared across invocations within the same runtime instance
_session_store = SessionStore()


def create_agent() -> Agent:
    """Initialize the Requirement Gathering Agent for the AgentCore runtime."""
    model = BedrockModel(
        model_id=BEDROCK_MODEL_ID,
        region_name=AWS_REGION,
    )

    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            lookup_ticket,
            get_ticket_requirements,
            validate_naming,
            validate_plan_against_policy,
        ],
    )


# Lazily initialized agent instance
_agent: Agent | None = None


def get_agent() -> Agent:
    """Return the singleton agent instance."""
    global _agent
    if _agent is None:
        _agent = create_agent()
    return _agent


def handler(event: dict, context: dict) -> dict:
    """AgentCore runtime handler.

    Receives an event from the AgentCore orchestration layer containing the
    user message and session metadata, processes it through the agent, and
    returns the response.

    Args:
        event: The incoming event with fields:
            - session_id: The active session identifier
            - user_message: The user's text input
            - ticket_id: (optional) A JIRA ticket to auto-load
        context: Runtime context (AgentCore metadata, identity info)

    Returns:
        A dictionary with the agent's response and updated session state.
    """
    session_id = event.get("session_id", "unknown")
    user_message = event.get("user_message", "")
    ticket_id = event.get("ticket_id")

    logger.info("Processing request for session %s", session_id)

    # Load existing session state if available
    session_state = _session_store.load(session_id) or {
        "messages": [],
        "turn_count": 0,
        "current_stage": "gathering",
    }

    # If a ticket_id is provided on the first turn, auto-load it
    if ticket_id and session_state["turn_count"] == 0:
        user_message = (
            f"I have a new provisioning request in ticket {ticket_id}. "
            f"Please look it up and help me gather the remaining requirements."
        )

    agent = get_agent()
    response = agent(user_message)
    assistant_message = str(response)

    session_state["messages"].append({"role": "user", "content": user_message})
    session_state["messages"].append({"role": "assistant", "content": assistant_message})
    session_state["turn_count"] += 1

    # Check for handoff readiness
    handoff_ready = "HANDOFF_READY" in assistant_message
    if handoff_ready:
        session_state["current_stage"] = "handoff_ready"

    _session_store.save(session_id, session_state)

    return {
        "session_id": session_id,
        "response": assistant_message,
        "turn_count": session_state["turn_count"],
        "stage": session_state["current_stage"],
        "handoff_ready": handoff_ready,
    }
