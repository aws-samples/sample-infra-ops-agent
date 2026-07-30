# Security Controls — Infrastructure Provisioning Agent

This document is the authoritative mapping of the security review findings to
the controls implemented in this sample and the hardening you must apply before
production use. It complements the [threat model](THREAT_MODEL.md) and the
[README security section](../README.md#security).

Each finding lists: **the risk**, **what this repo now provides**, and **what
you own** for a production deployment. "Provided" means the control ships as
code in this repo (IaC or application logic); "Guidance" means the control is
documented and parameterized but must be enabled/scoped for your environment.

> This remains **sample code**. The controls below make the intended secure
> architecture explicit and deployable; you are still responsible for validating
> them against your organization's standards before production use.

---

## High-severity findings

### H1 — Encryption at rest (SSE-KMS)
- **Risk:** Session data (DynamoDB), code artifacts (S3), and container images
  (ECR) encrypted only with AWS-owned keys — no independent key control, audit,
  or revocation.
- **Provided:** A customer-managed KMS key (`aws_kms_key.agent_data`, rotation
  enabled) now encrypts the DynamoDB session table, the S3 code bucket
  (SSE-KMS + bucket key), the ECR repository, and the CloudTrail bucket. Agent
  and CodeBuild roles are granted scoped `kms:Decrypt`/`GenerateDataKey` bound
  to the relevant service via `kms:ViaService`.
- **You own:** Decide on a dedicated key per data class if required by policy;
  set a key policy that restricts administrative access to your security team.
- **Where:** `infrastructure/main.tf`, `infrastructure/agentcore-deploy.tf`.

### H2 — Encryption in transit (TLS)
- **Risk:** No HTTPS enforcement; the portal ALB served plaintext HTTP and there
  was no `aws:SecureTransport` deny.
- **Provided:** The portal ALB now terminates **HTTPS only** with an ACM
  certificate on a TLS 1.3/1.2 policy (`ELBSecurityPolicy-TLS13-1-2-2021-06`);
  port 80 exists solely to `301`-redirect to HTTPS. The S3 code and CloudTrail
  buckets deny any request where `aws:SecureTransport = false`.
- **You own:** Provide the ACM `CertificateArn` parameter; scope
  `AllowedIngressCidr` to your networks.
- **Where:** `infrastructure/portal-fargate-alb.yaml`,
  `infrastructure/agentcore-deploy.tf`, `infrastructure/security.tf`.

### H3 — Prompt injection mitigation / Bedrock Guardrails
- **Risk:** The agent accepts free-text infrastructure requests with no content
  filtering, denied topics, PII handling, or prompt-injection defense.
- **Provided:** An `aws_bedrock_guardrail` with a `PROMPT_ATTACK` filter (HIGH),
  a `MISCONDUCT` filter, a denied topic for credential/policy exfiltration, and
  PII anonymization (email, secret keys, passwords). Both agents attach the
  guardrail to every model invocation when `BEDROCK_GUARDRAIL_ID` is set, and
  the execution role is granted `bedrock:ApplyGuardrail`.
- **You own:** Tune filters/denied topics to your policy; publish and pin a
  guardrail version; add regression tests for known injection payloads.
- **Where:** `infrastructure/security.tf`, `agents/*.py`, `config/__init__.py`.

### H4 — Human-in-the-loop approval gate
- **Risk:** The agent could trigger real builds autonomously; approval was
  prompt-guidance only and thus bypassable by a model error or prompt injection.
- **Provided:** An **in-code** approval gate (`tools/approval.py`) that
  `invoke_awx_template` calls before launching any job. Tokens are HMAC-signed,
  bound to a hash of the exact plan, single-use, and time-limited (15 min
  default). The gate **fails closed**: no valid token → no build, regardless of
  prompt content. Enabled by default (`REQUIRE_HUMAN_APPROVAL`).
- **You own:** Wire `issue_approval_token` to your human-facing control (portal
  button, JIRA transition, ChatOps); source `APPROVAL_SIGNING_SECRET` from
  Secrets Manager; back the single-use store with DynamoDB/Redis for multi-replica.
- **Where:** `tools/approval.py`, `tools/awx_tools.py`,
  `agents/provisioning_agent.py`.

### H5 — WAF protection
- **Risk:** The public ALB had no WAF, rate limiting, or managed rule coverage.
- **Provided:** An `AWS::WAFv2::WebACL` associated with the ALB, using the AWS
  Common and Known-Bad-Inputs managed rule groups plus a rate-based rule
  (500 requests / 5 min / IP → block).
- **You own:** Tune the rate limit and add geo/IP match rules for your exposure;
  consider AWS Shield Advanced if warranted.
- **Where:** `infrastructure/portal-fargate-alb.yaml`.

### H6 — Threat model
- **Risk:** No documented threat model (required per PCSR runbook).
- **Provided:** A STRIDE [threat model](THREAT_MODEL.md) covering the key threats
  (prompt injection, privilege escalation, data exfiltration, cost/capacity DoS)
  mapped to the controls in this document.
- **You own:** Extend the model for your deployment (e.g., in Threat Composer)
  before production.
- **Where:** `docs/THREAT_MODEL.md`.

### H7 — VPC endpoints
- **Risk:** Bedrock/DynamoDB/Secrets Manager API calls could traverse the public
  internet.
- **Provided:** Interface endpoints for `bedrock-runtime`, `bedrock-agentcore`,
  `secretsmanager`, `ecr.api`, `ecr.dkr`, `logs`, and `monitoring`, plus gateway
  endpoints for DynamoDB and S3, all with private DNS and an SG scoped to the VPC
  CIDR. Created only when a `vpc_id` + `private_subnet_ids` are supplied.
- **You own:** Supply your VPC/subnet IDs; add a VPC endpoint policy to restrict
  which principals/resources may use each endpoint.
- **Where:** `infrastructure/security.tf`.

### H8 — Architecture diagram completeness
- **Risk:** The diagram omitted Region, AZ boundaries, public/private subnet
  placement, and encrypted-data-flow indicators.
- **Provided:** An updated diagram in the [README](../README.md#architecture)
  showing the AWS Region, two AZs, public vs. private subnets, VPC endpoints,
  WAF, HTTPS/TLS edges, and KMS-encrypted data stores.
- **Where:** `README.md`.

---

## Medium-severity findings

### M1 — CloudTrail data events
- **Risk:** Only CloudWatch logging; no data-event audit for DynamoDB, S3, or
  Bedrock.
- **Provided:** A multi-region `aws_cloudtrail` with log-file validation, KMS
  encryption, and advanced event selectors for DynamoDB table data events, S3
  object data events (code bucket), and Bedrock invocation data events.
- **Where:** `infrastructure/security.tf`.

### M2 — DynamoDB backup / deletion protection
- **Risk:** No point-in-time recovery (PITR) or deletion protection on the
  session store.
- **Provided:** `point_in_time_recovery` enabled and
  `deletion_protection_enabled = true` on the session table.
- **Where:** `infrastructure/main.tf`.

### M3 — IAM least privilege
- **Risk:** "Write access to AWX/JIRA" was described without scoping.
- **Provided:** Roles avoid action wildcards; `ec2:RunInstances` is `Deny`-gated
  to `t3.micro`/`t3.small` and pinned to the deploy Region; Secrets access is
  scoped to the two specific secret ARNs; KMS use is bound by `kms:ViaService`;
  the requirement-gathering agent holds read-only tools while only the
  provisioning agent can execute. See the least-privilege matrix below.
- **You own:** Further constrain `ec2:RunInstances` by subnet/VPC condition keys
  and tag policies for your account.
- **Where:** `infrastructure/main.tf`, `infrastructure/security.tf`,
  `infrastructure/portal-fargate-alb.yaml`.

### M4 — Credential management
- **Risk:** AWX/JIRA/ServiceNow tokens passed as plain environment variables.
- **Provided:** Secrets Manager secrets for JIRA and AWX (KMS-encrypted); the
  config loader resolves tokens from Secrets Manager via `*_SECRET_ID` at
  runtime using the task role, falling back to env vars only for local dev.
- **You own:** Populate the secret values; remove plaintext tokens from `.env`
  in any shared/deployed environment; add ServiceNow to Secrets Manager likewise.
- **Where:** `config/__init__.py`, `.env.example`, `infrastructure/security.tf`.

### M5 — Network segmentation
- **Risk:** Public vs. private subnet placement was unspecified.
- **Provided:** Explicit `public_subnet_ids` (ALB only) and `private_subnet_ids`
  (runtime + endpoints) inputs; the diagram shows the split; the WAF/ALB sit at
  the public edge while data-plane traffic uses private subnets + VPC endpoints.
- **You own:** Map these inputs to your VPC; ensure the Fargate service runs in
  private subnets with egress via NAT/endpoints (the sample template's
  `AssignPublicIp` should be `DISABLED` once private subnets + endpoints exist).
- **Where:** `infrastructure/security.tf`, `README.md`.

### M6 — Pre-approved LLM
- **Risk:** "Claude via Amazon Bedrock" not confirmed against the pre-approved
  LLM list.
- **Confirmation:** The workload uses **Anthropic Claude Sonnet 4.6** on Amazon
  Bedrock (`us.anthropic.claude-sonnet-4-6`), a first-party managed foundation
  model on the Bedrock pre-approved list, invoked through a cross-region system
  inference profile. The model ID is version-pinned via `BEDROCK_MODEL_ID` to
  prevent uncontrolled drift.
- **You own:** Re-confirm against your org's current approved-model list at
  deploy time and record the approval reference.

### M7 — Rate limiting / abuse prevention
- **Risk:** No throttling or usage quotas on the agent API.
- **Provided:** The WAF rate-based rule (see H5) throttles per-IP request rate;
  the executor caps instances per request (max 3) and conversational turns
  (max 20).
- **You own:** Add per-user/token quotas (e.g., API Gateway usage plans or an
  application-level limiter) and Bedrock invocation budgets/alarms.
- **Where:** `infrastructure/portal-fargate-alb.yaml`.

---

## IAM least-privilege matrix (M3)

| Principal | Can do | Cannot do |
|---|---|---|
| Requirement-gathering agent role | Read JIRA, read/write its DynamoDB session items, invoke Bedrock (+ guardrail), use CMK via DynamoDB | Launch AWX jobs, run EC2, read secrets it doesn't own |
| Provisioning agent / execution role | Invoke Bedrock (+ guardrail), session DynamoDB, read the two named secrets, pull ECR, launch AWX **only with an approval token** | Run EC2 outside `t3.micro/small`, use CMK outside DynamoDB/Secrets Manager, wildcard actions |
| Portal task role | Invoke Bedrock, read code from S3, describe/tag EC2, `RunInstances` (Region-pinned, type-restricted) | Run non-`t3.micro/small` instances (explicit `Deny`), delete resources |
| CodeBuild role | Build/push the image, read/write the code bucket, use CMK for build artifacts | Touch runtime data stores or secrets |

---

## Enabling the controls

```bash
cd infrastructure

# Supply your network + certificate inputs (public ALB, private data plane).
terraform apply \
  -var="vpc_id=vpc-xxxxxxxx" \
  -var='private_subnet_ids=["subnet-aaa","subnet-bbb"]' \
  -var='public_subnet_ids=["subnet-ccc","subnet-ddd"]'

# Wire the guardrail + secrets into the agents (from terraform output):
#   BEDROCK_GUARDRAIL_ID / BEDROCK_GUARDRAIL_VERSION
#   JIRA_SECRET_ID / AWX_SECRET_ID
#   APPROVAL_SIGNING_SECRET (store in Secrets Manager)

# Deploy the portal with HTTPS + WAF:
aws cloudformation deploy \
  --template-file portal-fargate-alb.yaml \
  --stack-name infra-agent-portal \
  --parameter-overrides CertificateArn=arn:aws:acm:...:certificate/... \
  --capabilities CAPABILITY_IAM
```
