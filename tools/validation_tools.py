"""Validation tools for provisioning plans.

These tools validate proposed infrastructure plans against organizational
policies, naming conventions, and AWS guidelines before execution.
"""

import re

from strands import tool

from config import load_taxonomy


@tool
def validate_naming(hostname: str, environment: str, project: str, role: str) -> dict:
    """Validate a proposed hostname against the organization's naming convention.

    The naming pattern is: {env}-{project}-{role}-{seq:03d}
    Maximum length is 63 characters.

    Args:
        hostname: The proposed hostname to validate.
        environment: The target environment (development, staging, production).
        project: The project identifier.
        role: The server role (web, app, db, etc.).

    Returns:
        A dictionary with validation result, any violations found, and a
        suggested correction if the hostname is invalid.
    """
    taxonomy = load_taxonomy()
    naming_config = taxonomy["naming"]
    max_length = naming_config["max_length"]
    pattern_template = naming_config["pattern"]

    violations = []

    if len(hostname) > max_length:
        violations.append(
            f"Hostname exceeds maximum length of {max_length} characters "
            f"(current: {len(hostname)})."
        )

    expected_prefix = f"{environment[:3]}-{project}-{role}-"
    if not hostname.startswith(expected_prefix[:20]):
        violations.append(
            f"Hostname does not follow the naming pattern. "
            f"Expected prefix: '{expected_prefix}'"
        )

    if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", hostname):
        violations.append(
            "Hostname must contain only lowercase letters, digits, and hyphens, "
            "and must start and end with an alphanumeric character."
        )

    suggested = None
    if violations:
        env_short = environment[:3].lower()
        suggested = f"{env_short}-{project.lower()}-{role.lower()}-001"
        if len(suggested) > max_length:
            suggested = suggested[:max_length]

    return {
        "hostname": hostname,
        "valid": len(violations) == 0,
        "violations": violations,
        "suggested_correction": suggested,
    }


@tool
def validate_plan_against_policy(plan: dict) -> dict:
    """Validate a complete provisioning plan against organizational policies.

    Checks the plan against:
    - Allowed instance types, operating systems, and environments
    - Required tags (project, environment, owner, cost-center)
    - Backup and encryption policies
    - Network configuration validity
    - Storage constraints

    Args:
        plan: The provisioning plan dictionary containing fields such as
            instance_type, environment, operating_system, vpc, subnet,
            storage_size_gb, tags, etc.

    Returns:
        A dictionary with the overall validation status, a list of violations,
        and a list of warnings (non-blocking but recommended fixes).
    """
    taxonomy = load_taxonomy()
    violations = []
    warnings = []

    # Instance type
    if plan.get("instance_type") and plan["instance_type"] not in taxonomy["compute"]["instance_types"]:
        violations.append(
            f"Instance type '{plan['instance_type']}' is not in the approved list. "
            f"Allowed: {taxonomy['compute']['instance_types']}"
        )

    # Operating system
    if plan.get("operating_system") and plan["operating_system"] not in taxonomy["compute"]["operating_systems"]:
        violations.append(
            f"OS '{plan['operating_system']}' is not approved. "
            f"Allowed: {taxonomy['compute']['operating_systems']}"
        )

    # Environment
    if plan.get("environment") and plan["environment"] not in taxonomy["compute"]["environments"]:
        violations.append(
            f"Environment '{plan['environment']}' is not valid. "
            f"Allowed: {taxonomy['compute']['environments']}"
        )

    # VPC
    if plan.get("vpc") and plan["vpc"] not in taxonomy["networking"]["vpcs"]:
        violations.append(
            f"VPC '{plan['vpc']}' is not recognized. "
            f"Allowed: {taxonomy['networking']['vpcs']}"
        )

    # Required tags
    tags = plan.get("tags", {})
    required_tags = taxonomy["compliance"]["required_tags"]
    missing_tags = [t for t in required_tags if t not in tags]
    if missing_tags:
        violations.append(f"Missing required tags: {missing_tags}")

    # Storage
    storage = plan.get("storage_size_gb")
    if storage:
        limits = taxonomy["storage"]["sizes_gb"]
        if storage < limits["min"] or storage > limits["max"]:
            violations.append(
                f"Storage size {storage} GB is outside the allowed range "
                f"({limits['min']}–{limits['max']} GB)."
            )

    # Encryption (accept both field names)
    if taxonomy["compliance"]["encryption_at_rest_required"]:
        encryption_on = plan.get("encryption_enabled", False) or plan.get("encryption_at_rest", False)
        if not encryption_on:
            violations.append("Encryption at rest is required but not enabled in the plan.")

    # Backup policy
    if taxonomy["compliance"]["backup_policy_required"]:
        if not plan.get("backup_policy"):
            warnings.append("No backup policy specified. A default daily backup will be applied.")

    # Security group
    sg = plan.get("security_group")
    if sg and sg not in taxonomy["networking"]["security_group_templates"]:
        warnings.append(
            f"Security group template '{sg}' is not in the standard list. "
            f"It will be reviewed by the security team."
        )

    return {
        "valid": len(violations) == 0,
        "violations": violations,
        "warnings": warnings,
        "plan_summary": {
            "environment": plan.get("environment"),
            "instance_type": plan.get("instance_type"),
            "os": plan.get("operating_system"),
            "vpc": plan.get("vpc"),
        },
    }
