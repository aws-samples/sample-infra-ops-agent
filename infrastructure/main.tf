# Terraform configuration for the Infrastructure Agent support resources.
# This creates the DynamoDB session table and IAM roles used by the agents.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  description = "AWS Region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "dev"
}

# Customer-managed KMS key for encrypting session data at rest (SSE-KMS).
# Addresses H1: DynamoDB session data must be encrypted with a CMK (not just the
# default AWS-owned key) so that key access is auditable via CloudTrail and can
# be revoked/rotated independently of the AWS-managed key.
resource "aws_kms_key" "agent_data" {
  description             = "CMK for Infrastructure Agent session data at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = {
    Project     = "infrastructure-agent"
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

resource "aws_kms_alias" "agent_data" {
  name          = "alias/infrastructure-agent-${var.environment}"
  target_key_id = aws_kms_key.agent_data.key_id
}

# DynamoDB table for session persistence
resource "aws_dynamodb_table" "agent_sessions" {
  name         = "infrastructure-agent-sessions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"

  attribute {
    name = "session_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # H1: encrypt session data at rest with the customer-managed KMS key.
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.agent_data.arn
  }

  # M2: point-in-time recovery so session/conversation state can be restored.
  point_in_time_recovery {
    enabled = true
  }

  # M2: deletion protection prevents accidental drop of the session store.
  # Disable explicitly (with review) before any intentional teardown.
  deletion_protection_enabled = true

  tags = {
    Project     = "infrastructure-agent"
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# IAM role for the Requirement Gathering Agent (read-only JIRA, DynamoDB)
resource "aws_iam_role" "requirement_agent_role" {
  name = "infrastructure-agent-requirement-gathering-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "bedrock.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project     = "infrastructure-agent"
    Environment = var.environment
    AgentRole   = "requirement-gathering"
  }
}

resource "aws_iam_role_policy" "requirement_agent_dynamodb" {
  name = "dynamodb-session-access"
  role = aws_iam_role.requirement_agent_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
          "dynamodb:Scan",
        ]
        Resource = aws_dynamodb_table.agent_sessions.arn
      },
      # H1: allow the agent to use the CMK for DynamoDB SSE-KMS operations.
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = aws_kms_key.agent_data.arn
        Condition = {
          StringEquals = {
            "kms:ViaService" = "dynamodb.${var.aws_region}.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "requirement_agent_bedrock" {
  name = "bedrock-invoke"
  role = aws_iam_role.requirement_agent_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/*"
      }
    ]
  })
}

# IAM role for the Provisioning Agent (write access to JIRA, AWX, DynamoDB)
resource "aws_iam_role" "provisioning_agent_role" {
  name = "infrastructure-agent-provisioning-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "bedrock.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project     = "infrastructure-agent"
    Environment = var.environment
    AgentRole   = "provisioning"
  }
}

resource "aws_iam_role_policy" "provisioning_agent_dynamodb" {
  name = "dynamodb-session-access"
  role = aws_iam_role.provisioning_agent_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
        ]
        Resource = aws_dynamodb_table.agent_sessions.arn
      },
      # H1: allow the agent to use the CMK for DynamoDB SSE-KMS operations.
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = aws_kms_key.agent_data.arn
        Condition = {
          StringEquals = {
            "kms:ViaService" = "dynamodb.${var.aws_region}.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "provisioning_agent_bedrock" {
  name = "bedrock-invoke"
  role = aws_iam_role.provisioning_agent_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/*"
      }
    ]
  })
}

# CloudWatch log group for agent observability
resource "aws_cloudwatch_log_group" "agent_logs" {
  name              = "/infrastructure-agent/${var.environment}"
  retention_in_days = 90

  tags = {
    Project     = "infrastructure-agent"
    Environment = var.environment
  }
}

output "dynamodb_table_name" {
  value = aws_dynamodb_table.agent_sessions.name
}

output "kms_key_arn" {
  description = "Customer-managed KMS key encrypting session data at rest"
  value       = aws_kms_key.agent_data.arn
}

output "requirement_agent_role_arn" {
  value = aws_iam_role.requirement_agent_role.arn
}

output "provisioning_agent_role_arn" {
  value = aws_iam_role.provisioning_agent_role.arn
}

output "cloudwatch_log_group" {
  value = aws_cloudwatch_log_group.agent_logs.name
}
