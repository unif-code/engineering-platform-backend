"""Keep actual probe facts on expiry without allowing a late worker to reclaim a result."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest

from control_plane.app.modules.model_gateway.adapters import probe as probe_module
from control_plane.app.modules.model_gateway.application.worker import (
    process_connection_check,
    recover_expired_checks,
)
from control_plane.app.modules.model_gateway.domain.checks import (
    CheckReason,
    CheckState,
    InputCurrentness,
    ProbeOutcome,
)
from control_plane.app.modules.model_gateway.domain.connections import (
    EXECUTION_LEASE_SECONDS,
    CheckKind,
)
from tests.model_gateway.dispatch_support import check_dispatch
from tests.model_gateway.test_tool_call_checks import tool_http as tool_http


@pytest.mark.parametrize("limiter", ["network", "lease"])
def test_independent_get_retains_tool_facts_when_cleanup_exhausts_budget(
    tool_http: Any, monkeypatch: pytest.MonkeyPatch, limiter: str
) -> None:
    h = tool_http
    h.provider.value["usage"] = {
        "prompt_tokens": 3,
        "completion_tokens": 7,
        "total_tokens": 10,
    }
    body = json.dumps(h.provider.value).encode()

    class SlowClose(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield body

        async def aclose(self) -> None:
            await asyncio.sleep(0.4)

    h.provider.stream = SlowClose()
    if limiter == "network":
        monkeypatch.setattr(probe_module, "PROBE_TIMEOUT_SECONDS", 0.15)
    receipt = h.client.post(h.path, json={"checkKind": "TOOL_CALL"}, headers=h.headers)
    assert receipt.status_code == 202
    identifier = receipt.json()["id"]
    epoch: datetime = h.now
    origin, offset = time.monotonic(), 0.0

    def now() -> datetime:
        return epoch + timedelta(seconds=time.monotonic() - origin + offset)

    h.worker = replace(h.worker, common=replace(h.common, now=now))
    original_begin, shifted = h.store.begin, False

    @contextmanager
    def begin() -> Iterator[object]:
        nonlocal shifted, offset
        with original_begin() as db:
            yield db
        if not shifted and h.store.checks[identifier].state is CheckState.RUNNING:
            shifted = True
            offset = EXECUTION_LEASE_SECONDS - 0.15 if limiter == "lease" else 0.0

    h.store.begin = begin
    outcomes: list[ProbeOutcome] = []
    original_send = h.probe.send

    def send(*args: Any, **kwargs: Any) -> ProbeOutcome:
        outcome: ProbeOutcome = original_send(*args, **kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(h.probe, "send", send)
    assert process_connection_check(identifier, dependencies=h.worker)
    assert len(outcomes) == 1
    observed = outcomes[0]
    assert observed.state is CheckState.UNKNOWN
    assert observed.reason is CheckReason.RESPONSE_CLOSE_FAILED
    assert observed.observation is not None
    assert observed.elapsed_ms is not None and observed.usage is not None

    detail = h.client.get(h.path + "/" + identifier)
    assert detail.status_code == 200
    actual = detail.json()
    assert actual["state"] == "UNKNOWN"
    assert actual["reason"] == (
        "EXECUTION_EXPIRED" if limiter == "lease" else "RESPONSE_CLOSE_FAILED"
    )
    assert actual["currentness"] == ("UNVERIFIABLE" if limiter == "lease" else "CURRENT")
    assert actual["observation"] == {
        "kind": "TOOL_CALL",
        "consumedBytes": len(body),
        "matchingCallObserved": True,
        "toolCallCompletionObserved": True,
        "localResponseClosed": False,
    }
    assert actual["providerRequestId"] == observed.provider_request_id == h.provider.value["id"]
    assert actual["reportedModelId"] == observed.reported_model_id == "synthetic-model"
    assert actual["elapsedMs"] == observed.elapsed_ms
    assert actual["usage"] == {"promptTokens": 3, "completionTokens": 7, "totalTokens": 10}
    before = h.store.checks[identifier].model_dump_json()
    audit = deepcopy(h.store.audit)
    assert not process_connection_check(identifier, dependencies=h.worker)
    assert h.client.get(h.path).json()["items"] == [actual]
    assert h.client.get(h.path + "/" + identifier).json() == actual
    assert h.store.checks[identifier].model_dump_json() == before
    assert h.store.audit == audit and len(audit) == 2
    assert len(h.calls) == 1


@pytest.mark.parametrize("kind", list(CheckKind))
@pytest.mark.parametrize("expired", [False, True])
def test_expired_settlement_retains_real_adapter_facts_for_each_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: CheckKind, expired: bool
) -> None:
    h = check_dispatch(tmp_path, kind)
    original_send = h.probe.send
    outcomes: list[ProbeOutcome] = []

    def send(*args: Any, **kwargs: Any) -> ProbeOutcome:
        outcome: ProbeOutcome = original_send(*args, **kwargs)
        outcomes.append(outcome)
        if expired:
            h.now += timedelta(seconds=EXECUTION_LEASE_SECONDS + 1)
        return outcome

    monkeypatch.setattr(h.probe, "send", send)
    assert process_connection_check(h.identifier, dependencies=h.worker)
    assert len(outcomes) == 1
    observed = outcomes[0]
    assert observed.state is CheckState.SUCCEEDED
    assert observed.observation is not None and observed.elapsed_ms is not None
    final = h.service.get(h.store.candidate.id, h.identifier)
    assert final.state is (CheckState.UNKNOWN if expired else CheckState.SUCCEEDED)
    assert final.reason is (CheckReason.EXECUTION_EXPIRED if expired else None)
    assert final.material_currentness is (
        InputCurrentness.UNVERIFIABLE if expired else InputCurrentness.CURRENT
    )
    assert final.observation == observed.observation
    assert final.elapsed_ms == observed.elapsed_ms
    assert final.provider_request_id == observed.provider_request_id
    assert final.reported_model_id == observed.reported_model_id
    assert final.usage == observed.usage
    assert not process_connection_check(h.identifier, dependencies=h.worker)
    assert h.service.list(h.store.candidate.id, cursor=None, page_size=10) == ([final], None)
    assert len(h.store.audit) == 2
    assert len(h.events) == 1 and h.events[0]["transactions"] == 0


@pytest.mark.parametrize("kind", list(CheckKind))
def test_recovery_committed_before_late_outcome_keeps_row_history_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: CheckKind
) -> None:
    h = check_dispatch(tmp_path, kind)
    original_send = h.probe.send
    entered, release = Event(), Event()

    def send(*args: Any, **kwargs: Any) -> ProbeOutcome:
        outcome: ProbeOutcome = original_send(*args, **kwargs)
        assert outcome.state is CheckState.SUCCEEDED and outcome.observation is not None
        entered.set()
        if not release.wait(5):
            raise RuntimeError("completed probe barrier was not released")
        return outcome

    monkeypatch.setattr(h.probe, "send", send)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(process_connection_check, h.identifier, dependencies=h.worker)
        try:
            assert entered.wait(5)
            assert h.store.transactions == 0
            h.now += timedelta(seconds=EXECUTION_LEASE_SECONDS + 1)
            assert recover_expired_checks(dependencies=h.worker, limit=1) == 1
            recovered = h.service.get(h.store.candidate.id, h.identifier)
            assert recovered.state is CheckState.UNKNOWN
            assert recovered.reason is CheckReason.EXECUTION_EXPIRED
            assert recovered.observation is None and recovered.revision == 3
            before = recovered.model_dump_json()
            history = deepcopy(h.service.list(h.store.candidate.id, cursor=None, page_size=10))
            audit = deepcopy(h.store.audit)
        finally:
            release.set()
        assert pending.result(timeout=5)

    assert h.store.checks[h.identifier].model_dump_json() == before
    assert h.service.get(h.store.candidate.id, h.identifier).model_dump_json() == before
    assert h.service.list(h.store.candidate.id, cursor=None, page_size=10) == history
    assert h.store.audit == audit and len(audit) == 2
    assert not process_connection_check(h.identifier, dependencies=h.worker)
    assert len(h.events) == 1 and h.events[0]["transactions"] == 0
