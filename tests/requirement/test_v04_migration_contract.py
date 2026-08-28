from pathlib import Path


def test_v04_requirement_migration_declares_frozen_planning_facts_and_minimum_grants() -> None:
    source = Path("migrations/requirement/0005_requirement_sdd_human_gate.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "0005_req_sdd_human_gate"' in source
    assert 'down_revision = "0004_req_int_delivery"' in source
    assert "ADD COLUMN route_snapshot JSONB" in source
    assert "CREATE TABLE requirement.sdd_artifact_version" in source
    assert "CREATE TABLE requirement.work_item_assignment" in source
    assert "CREATE UNIQUE INDEX uq_requirement_current_work_item_assignment" in source
    assert "ADD COLUMN policy_code TEXT" in source
    assert "ADD COLUMN policy_snapshot_hash TEXT" in source
    assert "GRANT SELECT, INSERT ON requirement.sdd_artifact_version, " in source
    assert "requirement.work_item_assignment TO requirement_rw" in source
    assert "GRANT UPDATE (superseded_at)" in source
    assert "LOGIN PASSWORD" not in source.upper()
