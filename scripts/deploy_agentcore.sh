#!/usr/bin/env bash
# Deploy agents to Amazon Bedrock AgentCore Runtime.
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - bedrock-agentcore-starter-toolkit installed (pip install bedrock-agentcore-starter-toolkit)
#   - Docker running (for container builds)
#
# Usage:
#   ./scripts/deploy_agentcore.sh [--region us-east-1] [--env dev]

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --region) REGION="$2"; shift 2 ;;
        --env)    ENVIRONMENT="$2"; shift 2 ;;
        *)        echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "============================================================"
echo "Deploying Infrastructure Agent to Amazon Bedrock AgentCore"
echo "Region: ${REGION}  |  Environment: ${ENVIRONMENT}"
echo "============================================================"

# Configure the Requirement Gathering Agent
echo ""
echo "--- Configuring Requirement Gathering Agent ---"
agentcore configure \
    --agent-name "requirement-gathering-${ENVIRONMENT}" \
    --runtime-file runtime/requirement_gathering_runtime.py \
    --handler handler \
    --region "${REGION}" \
    --model-id "${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-6-v1_0}" \
    --max-turns 20

# Configure the Provisioning Agent
echo ""
echo "--- Configuring Provisioning Agent ---"
agentcore configure \
    --agent-name "provisioning-${ENVIRONMENT}" \
    --runtime-file runtime/provisioning_runtime.py \
    --handler handler \
    --region "${REGION}" \
    --model-id "${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-6-v1_0}" \
    --max-turns 10

# Launch both agents
echo ""
echo "--- Launching Agents ---"
agentcore launch --agent-name "requirement-gathering-${ENVIRONMENT}" --wait
agentcore launch --agent-name "provisioning-${ENVIRONMENT}" --wait

echo ""
echo "============================================================"
echo "Deployment complete."
echo ""
echo "Requirement Gathering Agent: requirement-gathering-${ENVIRONMENT}"
echo "Provisioning Agent:          provisioning-${ENVIRONMENT}"
echo ""
echo "Agent traces will appear in CloudWatch:"
echo "  /infrastructure-agent/${ENVIRONMENT}"
echo "============================================================"
