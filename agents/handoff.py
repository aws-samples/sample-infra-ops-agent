"""Multi-agent handoff coordinator.

Manages the context handoff between the Requirement Gathering Agent and the
Provisioning Agent. The gathered requirements are serialized as a structured
JSON payload that passes to the Provisioning Agent's context window.
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class ProvisioningRequirements:
    """Structured requirements payload passed between agents."""

    ticket_id: str
    environment: str
    instance_type: str
    operating_system: str
    vpc: str
    subnet: str
    security_group: str
    storage_size_gb: int
    project_name: str
    owner: str
    cost_center: str
    hostname: str
    region: str = "us-east-1"
    encryption_enabled: bool = True
    backup_policy: str = "daily"
    request_type: str = "ec2_linux"
    instance_count: int = 1
    tags: dict = field(default_factory=dict)
    additional_notes: Optional[str] = None

    def to_json(self) -> str:
        """Serialize the requirements to JSON for handoff."""
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, data: str) -> "ProvisioningRequirements":
        """Deserialize requirements from a JSON string."""
        return cls(**json.loads(data))

    def validate_completeness(self) -> list[str]:
        """Check that all required fields are populated.

        Returns:
            A list of field names that are empty or missing.
        """
        required_fields = [
            "ticket_id",
            "environment",
            "instance_type",
            "operating_system",
            "vpc",
            "subnet",
            "security_group",
            "storage_size_gb",
            "project_name",
            "owner",
            "cost_center",
            "hostname",
        ]
        missing = []
        for f in required_fields:
            value = getattr(self, f, None)
            if value is None or value == "":
                missing.append(f)
        return missing


def perform_handoff(requirements: ProvisioningRequirements) -> dict:
    """Execute the handoff from Requirement Gathering to Provisioning.

    Validates completeness, serializes the payload, and returns the context
    object that the Provisioning Agent will ingest.

    Args:
        requirements: The complete set of gathered requirements.

    Returns:
        A dictionary containing the serialized payload and handoff metadata.

    Raises:
        ValueError: If required fields are missing.
    """
    missing = requirements.validate_completeness()
    if missing:
        raise ValueError(
            f"Cannot hand off — missing required fields: {missing}"
        )

    payload = requirements.to_json()

    return {
        "handoff_type": "requirement_to_provisioning",
        "ticket_id": requirements.ticket_id,
        "payload": json.loads(payload),
        "field_count": len(asdict(requirements)),
        "ready": True,
    }
