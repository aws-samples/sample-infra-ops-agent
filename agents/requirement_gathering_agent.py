"""Requirement Gathering Agent — conversational intake for provisioning requests.

This agent uses the Strands Agents SDK to drive a multi-turn conversation that
replaces manual forms and spreadsheets. It identifies the provisioning request
from a JIRA ticket, asks targeted clarifying questions, and produces a validated
JSON requirements payload for the Provisioning Agent.

Usage (standalone/development):
    python -m agents.requirement_gathering_agent

In production this agent runs inside an Amazon Bedrock AgentCore Runtime
container (see runtime/requirement_gathering_runtime.py).
"""

import json
import sys
import uuid

from strands import Agent
from strands.models.bedrock import BedrockModel

from config import (
    AWS_REGION,
    BEDROCK_MODEL_ID,
    BEDROCK_GUARDRAIL_ID,
    BEDROCK_GUARDRAIL_VERSION,
)
from session.dynamodb_session import SessionStore
from tools.jira_tools import lookup_ticket, get_ticket_requirements
from tools.validation_tools import validate_naming, validate_plan_against_policy

SYSTEM_PROMPT = """\
You are a Requirement Gathering Agent for infrastructure provisioning. Your role
is to collect all information needed to provision cloud infrastructure by having
a natural language conversation with the requestor.

Your workflow:
1. When given a JIRA ticket ID, look it up and extract any pre-filled requirements.
2. Identify which fields are still missing from the provisioning schema.
3. Ask targeted, one-at-a-time follow-up questions to fill each missing field.
   Be specific: offer valid options (from the provisioning taxonomy) and explain
   what each choice means if the requestor is unfamiliar.
4. After all required fields are gathered, validate the proposed hostname against
   the naming convention and the overall plan against organizational policies.
5. Present a structured summary of the complete requirements for confirmation.
6. Once confirmed, serialize the requirements as JSON and signal that the
   handoff to the Provisioning Agent is ready.

Rules:
- Never guess values the user hasn't confirmed.
- If the user is unsure about a field, explain the options clearly.
- Maximum 20 conversational turns before escalating to a human operator.
- Always validate the plan against policies before producing the final payload.
"""

# Maximum turns before human escalation
MAX_TURNS = 20


def create_agent() -> Agent:
    """Create and return the Requirement Gathering Agent."""
    # H3: attach the Bedrock Guardrail to every model invocation when configured.
    model_kwargs = {"model_id": BEDROCK_MODEL_ID, "region_name": AWS_REGION}
    if BEDROCK_GUARDRAIL_ID:
        model_kwargs["guardrail_id"] = BEDROCK_GUARDRAIL_ID
        model_kwargs["guardrail_version"] = BEDROCK_GUARDRAIL_VERSION
    model = BedrockModel(**model_kwargs)

    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            lookup_ticket,
            get_ticket_requirements,
            validate_naming,
            validate_plan_against_policy,
        ],
    )
    return agent


def run_interactive():
    """Run the agent in interactive mode for local development."""
    agent = create_agent()
    session_store = SessionStore()
    session_id = str(uuid.uuid4())
    turn_count = 0

    print("=" * 60)
    print("Infrastructure Requirement Gathering Agent")
    print("Type a JIRA ticket ID to begin, or describe your request.")
    print("Type 'quit' to exit.")
    print("=" * 60)

    messages = []

    while turn_count < MAX_TURNS:
        user_input = input("\nYou: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            break

        turn_count += 1
        messages.append({"role": "user", "content": user_input})

        response = agent(user_input)
        assistant_message = str(response)

        messages.append({"role": "assistant", "content": assistant_message})
        print(f"\nAgent: {assistant_message}")

        session_store.save(
            session_id,
            {
                "messages": messages,
                "turn_count": turn_count,
                "current_stage": "gathering",
            },
        )

        if "HANDOFF_READY" in assistant_message:
            print("\n[Requirements gathered. Ready for handoff to Provisioning Agent.]")
            break

    if turn_count >= MAX_TURNS:
        print("\n[Maximum turns reached. Escalating to human operator.]")

    session_store.save(
        session_id,
        {
            "messages": messages,
            "turn_count": turn_count,
            "current_stage": "complete" if turn_count < MAX_TURNS else "escalated",
        },
    )


if __name__ == "__main__":
    run_interactive()
