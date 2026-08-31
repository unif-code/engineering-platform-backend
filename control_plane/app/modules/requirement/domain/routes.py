import hashlib
import json
from collections.abc import Mapping


def canonical_route_snapshot_hash(snapshot: Mapping[str, object]) -> str:
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
