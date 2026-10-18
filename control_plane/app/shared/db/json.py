import json
from typing import Any

from sqlalchemy import Connection, text


def materialize_jsonb(db: Connection, value: dict[str, Any]) -> dict[str, Any]:
    """Use the running PostgreSQL/driver representation before deriving a digest."""
    result = db.execute(
        text("SELECT CAST(:value AS JSONB)"), {"value": json.dumps(value)}
    ).scalar_one()
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result
