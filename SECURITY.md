# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, we ask that you
notify AWS/Amazon Security via our
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/)
or directly via email to aws-security@amazon.com.

Please do **not** create a public GitHub issue for security vulnerabilities.

## Scope and Intent

This repository contains **sample code intended for demonstration and learning
purposes**. It is not intended for production use as-is. Before deploying any
part of this solution, you should:

- Review all IAM policies and scope them to least privilege for your environment
- Replace all placeholder credentials and endpoints with your own
- Review network configuration (VPC, security groups, ingress rules) against
  your organization's security standards
- Run your own security scans against the code and any deployed infrastructure
- Work with your security and compliance teams to meet your organizational
  requirements

See the security attestation in the [README](README.md#security) for the scans
that have been run against this sample, the finding-by-finding control mapping
in [docs/SECURITY_CONTROLS.md](docs/SECURITY_CONTROLS.md), and the STRIDE
analysis in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).
