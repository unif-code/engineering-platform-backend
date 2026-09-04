from uuid import UUID

import pytest

from control_plane.app.modules.requirement.domain.evidence import (
    EvidenceCoverageConflict,
    RequirementDeliverySnapshot,
    canonical_acceptance_criteria_hash,
    canonical_delivery_snapshot_hash,
    validate_evidence_coverage,
)

REQUIREMENT_ID = "10000000-0000-0000-0000-000000000601"
WORK_ITEM_A = "20000000-0000-0000-0000-000000000601"
WORK_ITEM_B = "20000000-0000-0000-0000-000000000602"


def test_acceptance_criteria_hash_is_canonical_but_order_sensitive() -> None:
    first = canonical_acceptance_criteria_hash(("  first criterion  ", "second criterion"))
    normalized = canonical_acceptance_criteria_hash(("first criterion", "second criterion"))
    reordered = canonical_acceptance_criteria_hash(("second criterion", "first criterion"))

    assert first == normalized
    assert first.startswith("sha256:")
    assert first != reordered


def test_delivery_snapshot_hash_sorts_ids_and_rejects_duplicates() -> None:
    first = canonical_delivery_snapshot_hash(
        requirement_id=REQUIREMENT_ID,
        requirement_version=7,
        required_work_item_set_version=3,
        required_work_item_set_hash="sha256:" + "a" * 64,
        work_item_ids=(WORK_ITEM_B, WORK_ITEM_A),
    )
    second = canonical_delivery_snapshot_hash(
        requirement_id=REQUIREMENT_ID,
        requirement_version=7,
        required_work_item_set_version=3,
        required_work_item_set_hash="sha256:" + "a" * 64,
        work_item_ids=(WORK_ITEM_A, WORK_ITEM_B),
    )

    assert first == second
    assert first.startswith("sha256:")
    with pytest.raises(ValueError, match="duplicate WorkItem"):
        canonical_delivery_snapshot_hash(
            requirement_id=REQUIREMENT_ID,
            requirement_version=7,
            required_work_item_set_version=3,
            required_work_item_set_hash="sha256:" + "a" * 64,
            work_item_ids=(WORK_ITEM_A, WORK_ITEM_A),
        )


def test_delivery_snapshot_model_freezes_sorted_exact_set() -> None:
    snapshot = RequirementDeliverySnapshot.create(
        snapshot_id="30000000-0000-0000-0000-000000000601",
        requirement_id=REQUIREMENT_ID,
        requirement_version=7,
        required_work_item_set_version=3,
        required_work_item_set_hash="sha256:" + "b" * 64,
        work_item_ids=(WORK_ITEM_B, WORK_ITEM_A),
        created_by="employee-1",
    )

    assert snapshot.work_item_ids == (WORK_ITEM_A, WORK_ITEM_B)
    assert UUID(snapshot.id)
    assert snapshot.snapshot_hash == canonical_delivery_snapshot_hash(
        requirement_id=REQUIREMENT_ID,
        requirement_version=7,
        required_work_item_set_version=3,
        required_work_item_set_hash="sha256:" + "b" * 64,
        work_item_ids=(WORK_ITEM_A, WORK_ITEM_B),
    )


@pytest.mark.parametrize(
    ("actual", "message"),
    [
        ((WORK_ITEM_A,), "missing"),
        ((WORK_ITEM_A, WORK_ITEM_B, "20000000-0000-0000-0000-000000000603"), "extra"),
        ((WORK_ITEM_A, WORK_ITEM_A), "duplicate"),
    ],
)
def test_evidence_coverage_requires_one_to_one_exact_set(
    actual: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(EvidenceCoverageConflict, match=message):
        validate_evidence_coverage(
            required_work_item_ids=(WORK_ITEM_A, WORK_ITEM_B),
            evidence_work_item_ids=actual,
        )
