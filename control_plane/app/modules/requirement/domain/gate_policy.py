"""Requirement-owned, only-strengthening Gate policy. No runtime defaults."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

NAMESPACE = "requirement.gate"
SCHEMA_REVISION = 1
ACCEPTANCE_KEY = "acceptance.additional_required_capabilities"
FORMAL_KEY = "formal_review.additional_required_capabilities"
ARCHIVE_KEY = "draft_archive_after_days"
# A duration larger than the entire datetime range can never yield a valid cutoff.
MAX_ARCHIVE_DAYS = (date.max - date.min).days


def content_hash(values: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class GatePolicy:
    acceptance_additional: tuple[str, ...]
    formal_review_additional: tuple[str, ...]
    draft_archive_after_days: int
    acceptance_required_capabilities: tuple[str, ...] = field(init=False)
    formal_review_required_capabilities: tuple[str, ...] = field(init=False)
    acceptance_default_route: str = field(default="REQUIREMENT_CREATOR", init=False)
    formal_review_default_routes: tuple[tuple[str, str], ...] = field(
        default=(("MEMBER", "DIRECT_LEADER"), ("LEADER", "SELF")),
        init=False,
    )

    def __post_init__(self) -> None:
        for values in (self.acceptance_additional, self.formal_review_additional):
            if (
                type(values) is not tuple
                or any(type(value) is not str or value != "code.change" for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError("Capabilities must be an immutable supported set")
        if (
            type(self.draft_archive_after_days) is not int
            or not 1 <= self.draft_archive_after_days <= MAX_ARCHIVE_DAYS
        ):
            raise ValueError("Invalid draft archive interval")
        object.__setattr__(
            self,
            "acceptance_required_capabilities",
            ("requirement.acceptance.decide", *self.acceptance_additional),
        )
        object.__setattr__(
            self,
            "formal_review_required_capabilities",
            ("merge_request.review", *self.formal_review_additional),
        )

    @classmethod
    def parse(
        cls, values: dict[str, object], *, namespace: str, scope: str, schema_revision: int
    ) -> "GatePolicy":
        if (
            namespace != NAMESPACE
            or scope != "PLATFORM"
            or type(schema_revision) is not int
            or schema_revision != 1
        ):
            raise ValueError("Unsupported Gate policy identity")
        if set(values) != {ACCEPTANCE_KEY, FORMAL_KEY, ARCHIVE_KEY}:
            raise ValueError("Incomplete or unknown Gate policy keys")
        capabilities = []
        for key in (ACCEPTANCE_KEY, FORMAL_KEY):
            value = values[key]
            if (
                not isinstance(value, (list, tuple))
                or any(type(item) is not str or item != "code.change" for item in value)
                or len(set(value)) != len(value)
            ):
                raise ValueError("Invalid additional capability set")
            capabilities.append(tuple(sorted(value)))
        days = values[ARCHIVE_KEY]
        if type(days) is not int or not 1 <= days <= MAX_ARCHIVE_DAYS:
            raise ValueError("Invalid draft archive interval")
        return cls(capabilities[0], capabilities[1], days)

    def values(self) -> dict[str, object]:
        return {
            ACCEPTANCE_KEY: list(self.acceptance_additional),
            FORMAL_KEY: list(self.formal_review_additional),
            ARCHIVE_KEY: self.draft_archive_after_days,
        }

    def archive_cutoff(self, now: datetime) -> datetime:
        try:
            return now - timedelta(days=self.draft_archive_after_days)
        except OverflowError:
            raise ValueError("Archive cutoff is not representable") from None


@dataclass(frozen=True, slots=True)
class ResolvedGatePolicy:
    namespace: str
    scope: str
    schema_revision: int
    version: int
    snapshot_hash: str
    policy: GatePolicy
