# AgentCore deployment infrastructure — ECR, CodeBuild, IAM, and runtime
# This Terraform provisions the resources needed to deploy the Infrastructure
# Agent to Amazon Bedrock AgentCore Runtime.

# ECR Repository for the agent container image
resource "aws_ecr_repository" "agent" {
  name                 = "infra-provisioning-agent"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  # H1: encrypt the container image repository with the customer-managed CMK.
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.agent_data.arn
  }

  tags = {
    Project     = "infrastructure-agent"
    Environment = var.environment
  }
}

# ECR Lifecycle policy — keep last 10 images
resource "aws_ecr_lifecycle_policy" "agent" {
  repository = aws_ecr_repository.agent.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = {
        type = "expire"
      }
    }]
  })
}

# IAM Role for AgentCore Runtime execution
resource "aws_iam_role" "agentcore_execution" {
  name = "infra-agent-agentcore-execution-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = [
            "bedrock.amazonaws.com",
            "bedrock-agentcore.amazonaws.com"
          ]
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project     = "infrastructure-agent"
    Environment = var.environment
  }
}

# Bedrock model invocation permissions
resource "aws_iam_role_policy" "agentcore_bedrock" {
  name = "bedrock-invoke"
  role = aws_iam_role.agentcore_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/*"
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles"]
        Resource = "*"
      }
    ]
  })
}

# DynamoDB access for session persistence
resource "aws_iam_role_policy" "agentcore_dynamodb" {
  name = "dynamodb-session-access"
  role = aws_iam_role.agentcore_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
          "dynamodb:Scan"
        ]
        Resource = aws_dynamodb_table.agent_sessions.arn
      }
    ]
  })
}

# CloudWatch Logs for agent observability
resource "aws_iam_role_policy" "agentcore_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.agentcore_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/vendedlogs/bedrock-agentcore/*"
      }
    ]
  })
}

# ECR pull permissions (for AgentCore to pull the container)
resource "aws_iam_role_policy" "agentcore_ecr" {
  name = "ecr-pull"
  role = aws_iam_role.agentcore_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:GetAuthorizationToken"
        ]
        Resource = [
          aws_ecr_repository.agent.arn,
          "arn:aws:ecr:${var.aws_region}:*:repository/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      }
    ]
  })
}

# S3 bucket for direct code deploy (used by AgentCore starter toolkit)
resource "aws_s3_bucket" "agent_code" {
  bucket        = "infra-agent-code-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  force_destroy = true

  tags = {
    Project     = "infrastructure-agent"
    Environment = var.environment
  }
}

resource "aws_s3_bucket_versioning" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id
  versioning_configuration {
    status = "Enabled"
  }
}

# H1: encrypt the code bucket with the customer-managed CMK (SSE-KMS) and use
# an S3 Bucket Key to reduce KMS request cost.
resource "aws_s3_bucket_server_side_encryption_configuration" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.agent_data.arn
    }
    bucket_key_enabled = true
  }
}

# H2: enforce TLS in transit — deny any non-HTTPS request to the code bucket.
resource "aws_s3_bucket_policy" "agent_code_tls" {
  bucket = aws_s3_bucket.agent_code.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.agent_code.arn,
          "${aws_s3_bucket.agent_code.arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })
}

# H1: block all public access to the code bucket.
resource "aws_s3_bucket_public_access_block" "agent_code" {
  bucket                  = aws_s3_bucket.agent_code.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# S3 access for code deploy
resource "aws_iam_role_policy" "agentcore_s3" {
  name = "s3-code-access"
  role = aws_iam_role.agentcore_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.agent_code.arn,
          "${aws_s3_bucket.agent_code.arn}/*"
        ]
      },
      # H1: decrypt KMS-encrypted S3 objects and ECR image layers.
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = aws_kms_key.agent_data.arn
      }
    ]
  })
}

# CodeBuild role for building the container image
resource "aws_iam_role" "codebuild" {
  name = "infra-agent-codebuild-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "codebuild.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Project     = "infrastructure-agent"
    Environment = var.environment
  }
}

resource "aws_iam_role_policy" "codebuild_policy" {
  name = "codebuild-policy"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.agent_code.arn,
          "${aws_s3_bucket.agent_code.arn}/*"
        ]
      },
      # H1: encrypt/decrypt KMS-protected S3 objects and ECR layers during build.
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = aws_kms_key.agent_data.arn
      }
    ]
  })
}

data "aws_caller_identity" "current" {}

# Outputs
output "ecr_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}

output "agentcore_execution_role_arn" {
  value = aws_iam_role.agentcore_execution.arn
}

output "codebuild_role_arn" {
  value = aws_iam_role.codebuild.arn
}

output "s3_code_bucket" {
  value = aws_s3_bucket.agent_code.bucket
}

output "deploy_command" {
  value = <<-EOT
    agentcore deploy \
      --agent "infra_provisioning_agent" \
      --env "AWS_REGION=${var.aws_region}" \
      --env "BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6" \
      --auto-update-on-conflict
  EOT
}
