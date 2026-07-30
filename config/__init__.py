"""Configuration loader for the Infrastructure Agent."""

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

import yaml
from dotenv import load_dotenv

load_dotenv()

CONFIG_DIR = Path(__file__).parent


def load_taxonomy() -> dict:
    """Load the provisioning taxonomy from YAML."""
    with open(CONFIG_DIR / "taxonomy.yaml") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=8)
def _get_secret(secret_id: str) -> dict:
    """Fetch and cache a JSON secret from AWS Secrets Manager.

    M4 (credential management): external API tokens should never live in plain
    environment variables in a real deployment. Store them in AWS Secrets
    Manager and reference the secret by name/ARN; this resolves the secret at
    runtime using the task IAM role (no static credentials).
    """
    import boto3  # imported lazily so local dev without boto3/creds still works

    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    value = client.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        # Plain-string secret — expose it under a conventional "token" key.
        return {"token": value}


def _secret_field(secret_env_var: str, key: str, fallback_env_var: str) -> str:
    """Resolve a credential from Secrets Manager when configured, else env var.

    If ``secret_env_var`` names a secret (e.g. ``JIRA_SECRET_ID``), the value of
    ``key`` is read from that secret. Otherwise we fall back to the plain
    ``fallback_env_var`` so local development keeps working from ``.env``.
    """
    secret_id = os.getenv(secret_env_var, "")
    if secret_id:
        try:
            return _get_secret(secret_id).get(key, "")
        except Exception as exc:  # noqa: BLE001 — fall back to env for local/dev
            logger.warning(
                "Could not read %s from Secrets Manager (%s); falling back to %s.",
                secret_env_var,
                exc,
                fallback_env_var,
            )
    return os.getenv(fallback_env_var, "")


# AWS / Bedrock
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6-v1_0")

# Bedrock Guardrail (H3) — set to enforce content/prompt-injection filtering.
BEDROCK_GUARDRAIL_ID = os.getenv("BEDROCK_GUARDRAIL_ID", "")
BEDROCK_GUARDRAIL_VERSION = os.getenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")

# JIRA — token sourced from Secrets Manager (JIRA_SECRET_ID) when set (M4).
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "")
JIRA_API_TOKEN = _secret_field("JIRA_SECRET_ID", "api_token", "JIRA_API_TOKEN")
JIRA_USER_EMAIL = os.getenv("JIRA_USER_EMAIL", "")

# AWX — token sourced from Secrets Manager (AWX_SECRET_ID) when set (M4).
AWX_BASE_URL = os.getenv("AWX_BASE_URL", "")
AWX_API_TOKEN = _secret_field("AWX_SECRET_ID", "api_token", "AWX_API_TOKEN")

# DynamoDB
DYNAMODB_TABLE_NAME = os.getenv("DYNAMODB_TABLE_NAME", "infrastructure-agent-sessions")
DYNAMODB_REGION = os.getenv("DYNAMODB_REGION", AWS_REGION)
