from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast

import pytest

import control_plane.app.modules.requirement as requirement_facade
from control_plane.app.modules.source_control import (
    EvidenceStale,
    EvidenceUnavailable,
    SourceControlDependencies,
)
from control_plane.app.modules.source_control.adapters import requirement_evidence
from control_plane.app.modules.source_control.application import evidence


@pytest.mark.parametrize("stale_at", [1, 2], ids=("before-reading-items", "after-reading-items"))
def test_generation_rejects_changed_requirement_input_before_publishing(
    stale_at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks: list[dict[str, Any]] = []
    reads: list[str] = []
    request = {
        "requirement_id": "requirement-1",
        "requirement_version": 7,
        "required_work_item_set_version": 3,
        "required_work_item_set_hash": "sha256:" + "a" * 64,
        "work_item_ids": ["work-1"],
    }

    def validate_snapshot(**values: Any) -> None:
        checks.append(values)
        if len(checks) == stale_at:
            raise EvidenceStale("Requirement input changed")

    def read_item(*_args: Any, work_item_id: str, **_kwargs: Any) -> Any:
        assert len(checks) == 1, "Requirement input must be checked before evidence reads"
        reads.append(work_item_id)
        return object()

    monkeypatch.setattr(evidence, "_item_from_context", read_item)
    dependencies = cast(
        SourceControlDependencies,
        SimpleNamespace(requirement_evidence=SimpleNamespace(validate_snapshot=validate_snapshot)),
    )
    with pytest.raises(EvidenceStale, match="Requirement input changed"):
        evidence._generate_claimed_integration_baseline(
            cast(Any, object()),
            message_id="request-1",
            claimed=request,
            generated_by="SYSTEM:SOURCE_CONTROL",
            dependencies=dependencies,
        )

    assert reads == ([] if stale_at == 1 else ["work-1"])
    assert checks == [{**request, "work_item_ids": ("work-1",)}] * stale_at


@pytest.mark.parametrize(
    "changed",
    [
        {"requirement_id": "other-requirement"},
        {"requirement_version": 8},
        {"required_work_item_set_version": 4},
        {"required_work_item_set_hash": "sha256:" + "b" * 64},
        {"work_item_ids": ("work-1", "work-2")},
        {},
    ],
)
def test_snapshot_adapter_checks_live_requirement_facade_fields(
    changed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {
        "requirement_id": "requirement-1",
        "requirement_version": 7,
        "required_work_item_set_version": 3,
        "required_work_item_set_hash": "sha256:" + "a" * 64,
        "work_item_ids": ("work-1",),
    }
    calls = []

    def current(_db: Any, *, requirement_id: str, dependencies: Any) -> Any:
        calls.append(requirement_id)
        return SimpleNamespace(**{**expected, **changed})

    monkeypatch.setattr(requirement_facade, "get_requirement_delivery_snapshot", current)
    adapter = requirement_evidence.RequirementFacadeEvidenceAdapter(
        cast(Any, SimpleNamespace(connect=nullcontext)), object()
    )
    if changed:
        with pytest.raises(EvidenceStale):
            adapter.validate_snapshot(**expected)  # type: ignore[arg-type]
    else:
        adapter.validate_snapshot(**expected)  # type: ignore[arg-type]
    assert calls == ["requirement-1"]


def test_generation_fails_closed_without_requirement_owner() -> None:
    with pytest.raises(EvidenceUnavailable, match="Requirement delivery input unavailable"):
        evidence._validate_snapshot_request(
            {}, cast(SourceControlDependencies, SimpleNamespace(requirement_evidence=None))
        )
