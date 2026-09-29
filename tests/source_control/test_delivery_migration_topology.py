"""Offline migration planning for the independently integrated delivery branches."""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


@pytest.mark.parametrize(
    ("installed", "pending"),
    [
        (
            "0006_sc_mr_reconcile",
            (
                "0010_sc_agent_delivery",
                "0007_sc_evidence",
                "0008_sc_formal_delivery",
                "0011_sc_delivery_join",
            ),
        ),
        (
            "0008_sc_formal_delivery",
            ("0010_sc_agent_delivery", "0011_sc_delivery_join"),
        ),
        (
            "0010_sc_agent_delivery",
            ("0007_sc_evidence", "0008_sc_formal_delivery", "0011_sc_delivery_join"),
        ),
    ],
)
def test_delivery_join_plans_only_missing_revisions(
    installed: str, pending: tuple[str, ...]
) -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    steps = scripts._upgrade_revs("source_control@head", installed)
    assert tuple(step.revision.revision for step in steps) == pending
    join = scripts.get_revision("source_control@head")
    assert join is not None
    assert join.down_revision == ("0008_sc_formal_delivery", "0010_sc_agent_delivery")


@pytest.mark.parametrize(
    ("installed", "removed"),
    [
        ("0007_sc_evidence", ("0007_sc_evidence",)),
        ("0010_sc_agent_delivery", ("0010_sc_agent_delivery",)),
        (
            "0011_sc_delivery_join",
            (
                "0011_sc_delivery_join",
                "0008_sc_formal_delivery",
                "0007_sc_evidence",
                "0010_sc_agent_delivery",
            ),
        ),
    ],
)
def test_delivery_downgrade_targets_the_shared_branch_point(
    installed: str, removed: tuple[str, ...]
) -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    steps = scripts._downgrade_revs("0006_sc_mr_reconcile", installed)
    assert tuple(step.revision.revision for step in steps) == removed
