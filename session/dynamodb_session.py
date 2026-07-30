"""Session persistence backed by Amazon DynamoDB.

Allows users to resume requirement gathering across multiple sessions without
losing conversational context or partially-gathered specifications.
"""

import json
import time
from typing import Optional

import boto3

from config import DYNAMODB_TABLE_NAME, DYNAMODB_REGION


class SessionStore:
    """Persist and retrieve agent conversation state in DynamoDB."""

    def __init__(self, table_name: str = DYNAMODB_TABLE_NAME):
        self._dynamodb = boto3.resource("dynamodb", region_name=DYNAMODB_REGION)
        self._table = self._dynamodb.Table(table_name)

    def save(self, session_id: str, state: dict) -> None:
        """Save or update a session's state.

        Args:
            session_id: Unique identifier for the session.
            state: The session state to persist (messages, gathered
                requirements, current stage, etc.).
        """
        self._table.put_item(
            Item={
                "session_id": session_id,
                "state": json.dumps(state),
                "updated_at": int(time.time()),
                "ttl": int(time.time()) + 86400 * 30,  # 30-day expiry
            }
        )

    def load(self, session_id: str) -> Optional[dict]:
        """Load a session's state from DynamoDB.

        Args:
            session_id: The session to retrieve.

        Returns:
            The session state dictionary, or None if not found.
        """
        response = self._table.get_item(Key={"session_id": session_id})
        item = response.get("Item")
        if not item:
            return None
        return json.loads(item["state"])

    def delete(self, session_id: str) -> None:
        """Remove a session from the store."""
        self._table.delete_item(Key={"session_id": session_id})

    def list_active(self, limit: int = 50) -> list[dict]:
        """List recently-updated sessions (for admin/debug purposes)."""
        response = self._table.scan(Limit=limit)
        sessions = []
        for item in response.get("Items", []):
            state = json.loads(item["state"])
            sessions.append(
                {
                    "session_id": item["session_id"],
                    "updated_at": item["updated_at"],
                    "stage": state.get("current_stage", "unknown"),
                }
            )
        return sorted(sessions, key=lambda s: s["updated_at"], reverse=True)
