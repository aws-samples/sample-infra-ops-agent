# Threat Model — Infrastructure Provisioning Agent

This threat model follows the STRIDE methodology (Spoofing, Tampering,
Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege).
It documents the threats identified for this sample and the mitigations that are
either built in or recommended before any real-world deployment.

> This is **sample code for demonstration**. The mitigations marked
> _"Recommended"_ are the customer's responsibility to implement before
> production use.

## 1. Application Overview

A conversational infrastructure-provisioning solution. Users describe
infrastructure needs in natural language through a web portal. Two agents
(built on the Strands Agents SDK, hosted on Amazon Bedrock AgentCore Runtime)
gather requirements, validate them against an organizational taxonomy, and
trigger Infrastructure-as-Code (IaC) pipelines to provision AWS resources.

## 2. Architecture & Trust Boundaries

```
  [ Internet ]
       │  (TB1: public edge)
       ▼
  [ ALB ] ──▶ [ ECS Fargate: Portal ] ──▶ [ Amazon Bedrock (LLM) ]
                     │  (TB2: app → AWS APIs)
                     ├──▶ [ Amazon DynamoDB (sessions) ]
                     ├──▶ [ Amazon EC2 (provisioning via task IAM role) ]
                     ├──▶ [ JIRA API ]   (TB3: app → external SaaS)
                     └──▶ [ AWX API ]    (TB3: app → external SaaS)
```

- **TB1 — Public edge:** the ALB accepts unauthenticated traffic from the
  internet.
- **TB2 — Application to AWS control plane:** the portal/agent calls Bedrock,
  DynamoDB, and EC2 using its task IAM role.
- **TB3 — Application to external systems:** the agent calls JIRA and AWX with
  credentials supplied via environment variables.

## 3. Assets

| Asset | Why it matters |
|---|---|
| AWS credentials (task IAM role) | Can provision/describe EC2, invoke Bedrock, read/write DynamoDB |
| JIRA / AWX API tokens | Ticket data and IaC pipeline execution |
| Session state (DynamoDB) | Conversation history and gathered requirements |
| Provisioning capability | Ability to create real AWS infrastructure |

## 4. STRIDE Analysis

### Spoofing
| Threat | Mitigation |
|---|---|
| Unauthenticated user drives the portal and provisions resources | **Partial:** AWS WAF (managed rules + rate limiting) fronts the ALB and ingress can be CIDR-scoped, and provisioning still requires a human approval token. **Recommended (you own):** put the portal behind authentication (Amazon Cognito, ALB OIDC, or IAM auth on AgentCore Runtime) before exposing it publicly with real provisioning enabled. |
| Caller impersonates the agent to AWS APIs | **Built in:** the task uses an IAM role via the instance/task metadata service; no static keys are embedded. |

### Tampering
| Threat | Mitigation |
|---|---|
| **Prompt injection** — free-text input steers the agent to provision out-of-policy or exfiltrate data | **Built in:** (1) an Amazon Bedrock Guardrail with a HIGH `PROMPT_ATTACK` filter, denied topics, and PII masking is attached to every model invocation (H3); (2) every plan is validated against `config/taxonomy.yaml` before execution; (3) provisioning is gated by an in-code approval check that the model cannot bypass (H4). |
| Malicious input alters the provisioning plan | **Built in:** every plan is validated against `config/taxonomy.yaml` (allowed instance types, VPCs, OSes) and a naming convention before execution. |
| Container image tampering | **Built in:** ECR image scanning on push, immutable tags, KMS-encrypted repository. **Recommended:** enable image signing. |
| IaC template tampering in transit | **Built in:** app code is pulled from an SSE-KMS–encrypted, versioned, public-access-blocked S3 bucket over TLS (a bucket policy denies non-TLS access). |

### Repudiation
| Threat | Mitigation |
|---|---|
| A provisioning action cannot be attributed | **Built in:** AgentCore emits traces to Amazon CloudWatch; a multi-region CloudTrail with log-file validation captures API + data events for DynamoDB, S3, and Bedrock (M1); every provisioned instance is tagged `ProvisionedBy=infrastructure-agent`; the approval gate records the approver identity on each build; JIRA tickets record the request trail. |

### Information Disclosure
| Threat | Mitigation |
|---|---|
| **Data exfiltration** via model output or logs | **Built in:** the Bedrock Guardrail anonymizes PII (email, secret keys, passwords) and denies topics that solicit credentials/policies; model invocation traffic stays on the AWS network via VPC endpoints (H7). |
| Secrets committed to the repo | **Built in:** `.env`, `.bedrock_agentcore.yaml`, `*.pem`, and Terraform state are git-ignored; only `.env.example` with placeholders is published. |
| Secrets exposed in the container | **Built in:** JIRA/AWX tokens are sourced from AWS Secrets Manager at runtime via the task role (`*_SECRET_ID`); plain env vars remain only as a local-dev fallback (M4). |
| Session data at rest | **Built in:** DynamoDB SSE with a customer-managed, rotated KMS key; PITR + deletion protection; 30-day TTL on records (H1, M2). |
| Data in transit | **Built in:** all AWS SDK and external API calls use TLS; the ALB is HTTPS-only (TLS 1.3/1.2 + ACM) with HTTP redirecting to HTTPS; S3 buckets deny non-TLS access (H2). |

### Denial of Service
| Threat | Mitigation |
|---|---|
| Public ALB flooded with requests | **Built in:** AWS WAF (managed rules + a rate-based rule, 500 req/5 min/IP) fronts the ALB; ingress can be scoped via `AllowedIngressCidr` (H5, M7). |
| Runaway provisioning (cost/capacity DoS) | **Built in:** the executor caps instances per request (max 3) and the task IAM role denies any instance type other than `t3.micro`/`t3.small`; provisioning also requires human approval. |
| Agent loop exhaustion | **Built in:** the Requirement Gathering Agent caps conversational turns (max 20) before escalating to a human. |

### Elevation of Privilege
| Threat | Mitigation |
|---|---|
| **Privilege escalation** — agent provisions oversized or out-of-policy resources | **Built in:** IAM `Deny` on non-`t3.micro`/`t3.small` instance types and Region-pinned `RunInstances` — enforced at the AWS control plane, independent of application logic; the human approval gate must pass first. |
| Over-broad task permissions | **Built in:** roles avoid action wildcards and are scoped (Bedrock invoke + guardrail, DynamoDB on one table, EC2 run/describe/tag, SSM AMI read, two named secrets, CMK bound by `kms:ViaService`). **Recommended:** further constrain `ec2:RunInstances` by subnet/VPC condition keys and tag policies. |
| Requirement agent granted write access | **Built in:** the Requirement Gathering Agent has read-only tools; only the Provisioning Agent can invoke execution tools, and only with a valid approval token. |

## 5. Residual Risks (customer responsibility before production)

The encryption, TLS, guardrail, approval, WAF, VPC-endpoint, CloudTrail, and
Secrets Manager controls above now ship as code (see
[SECURITY_CONTROLS.md](SECURITY_CONTROLS.md)). The remaining items you must
address for your environment:

- **Authentication** — the sample portal has no user authentication in front of
  the ALB; put it behind Amazon Cognito, ALB OIDC, or IAM auth on AgentCore
  before exposing it with real provisioning enabled.
- **Guardrail / WAF tuning** — the shipped rules are sensible defaults; tune
  denied topics, filters, and rate limits to your policy and add injection
  regression tests.
- **Approval wiring** — connect `issue_approval_token` to your human control and
  source `APPROVAL_SIGNING_SECRET` from Secrets Manager.
- **IAM scoping** — further constrain `ec2:RunInstances` by subnet/VPC/tag
  conditions for your account.

Each is called out in [SECURITY.md](../SECURITY.md), the README, and
[SECURITY_CONTROLS.md](SECURITY_CONTROLS.md).

## 6. How to extend this model

Recreate or expand this model interactively using
[Threat Composer](https://awslabs.github.io/threat-composer/) — define the
application, import the architecture, and add/refine threats and mitigations for
your specific deployment before going to production.
