"""Exercise real worker/HTTP boundaries; these assertions also run against the pre-fix code."""

import asyncio
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event

import httpx
import pytest

from control_plane.app.modules.model_gateway.adapters import probe as probe_module
from control_plane.app.modules.model_gateway.application.worker import (
    process_connection_check,
    recover_expired_checks,
)
from control_plane.app.modules.model_gateway.domain.checks import CheckReason, CheckState
from control_plane.app.modules.model_gateway.domain.connections import (
    EXECUTION_LEASE_SECONDS,
    PROBE_TIMEOUT_SECONDS,
    CheckKind,
)
from tests.model_gateway.dispatch_support import check_dispatch, successful_response


@pytest.mark.parametrize("kind", list(CheckKind))
@pytest.mark.parametrize("mode", ["live", "expired-running", "recovered"])
def test_committed_claim_must_still_own_dispatch(
    tmp_path: Path, kind: CheckKind, mode: str
) -> None:
    h = check_dispatch(tmp_path, kind)
    entered, release = Event(), Event()
    original_begin = h.store.begin
    intercepted = False
    recovered_snapshot = None

    @contextmanager
    def begin() -> Iterator[object]:
        nonlocal intercepted
        with original_begin() as db:
            yield db
        if (
            mode != "live"
            and not intercepted
            and h.store.checks[h.identifier].state is CheckState.RUNNING
        ):
            intercepted = True
            entered.set()
            if not release.wait(5):
                raise RuntimeError("committed claim barrier was not released")

    h.store.begin = begin
    if mode == "live":
        assert process_connection_check(h.identifier, dependencies=h.worker)
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(process_connection_check, h.identifier, dependencies=h.worker)
            try:
                assert entered.wait(5)
                assert h.store.transactions == 0
                h.now += timedelta(seconds=EXECUTION_LEASE_SECONDS + 1)
                if mode == "recovered":
                    assert recover_expired_checks(dependencies=h.worker, limit=1) == 1
                    recovered_snapshot = h.store.checks[h.identifier].model_dump_json()
            finally:
                release.set()
            pending.result(timeout=5)
    final = h.store.checks[h.identifier]
    assert not process_connection_check(h.identifier, dependencies=h.worker)
    assert len(h.store.audit) == 2
    if recovered_snapshot is not None:
        assert final.model_dump_json() == recovered_snapshot
    if mode == "live":
        assert final.state is CheckState.SUCCEEDED
        assert h.events == [
            {
                "method": "POST",
                "state": CheckState.RUNNING,
                "revision": 2,
                "deadline_expired": False,
                "transactions": 0,
            }
        ]
    else:
        assert final.state is CheckState.UNKNOWN and final.reason is CheckReason.EXECUTION_EXPIRED
        assert h.events == [], "expired or recovered claim started a Provider POST"


@pytest.mark.parametrize("kind", list(CheckKind))
def test_recovery_during_client_preparation_is_checked_at_actual_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: CheckKind
) -> None:
    h = check_dispatch(tmp_path, kind)
    original_client = probe_module.model_https_client
    recovered_snapshot = None

    def client(transport: httpx.AsyncBaseTransport | None, timeout: float) -> httpx.AsyncClient:
        nonlocal recovered_snapshot
        value = original_client(transport, timeout)
        assert h.store.transactions == 0
        h.now += timedelta(seconds=EXECUTION_LEASE_SECONDS + 1)
        assert recover_expired_checks(dependencies=h.worker, limit=1) == 1
        recovered_snapshot = h.store.checks[h.identifier].model_dump_json()
        return value

    monkeypatch.setattr(probe_module, "model_https_client", client)
    process_connection_check(h.identifier, dependencies=h.worker)
    assert recovered_snapshot is not None
    assert h.store.checks[h.identifier].model_dump_json() == recovered_snapshot
    assert len(h.store.audit) == 2
    assert h.events == [], "claim recovered during client preparation still sent HTTP"


@pytest.mark.parametrize("pause", [0, 10])
def test_network_and_cleanup_budget_never_restarts_beyond_committed_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pause: int
) -> None:
    h = check_dispatch(tmp_path)
    original_begin = h.store.begin
    paused = False

    @contextmanager
    def begin() -> Iterator[object]:
        nonlocal paused
        with original_begin() as db:
            yield db
        if not paused and h.store.checks[h.identifier].state is CheckState.RUNNING:
            paused = True
            h.now += timedelta(seconds=pause)

    h.store.begin = begin
    original_timeout = asyncio.timeout_at
    deadlines: list[float] = []
    remaining: list[float] = []

    def timeout_at(deadline: float | None) -> asyncio.Timeout:
        assert deadline is not None
        if h.store.checks[h.identifier].state is CheckState.RUNNING:
            deadlines.append(deadline)
            remaining.append(deadline - asyncio.get_running_loop().time())
        return original_timeout(deadline)

    monkeypatch.setattr(asyncio, "timeout_at", timeout_at)
    assert process_connection_check(h.identifier, dependencies=h.worker)
    assert len(h.events) == 1 and h.store.checks[h.identifier].state is CheckState.SUCCEEDED
    assert remaining and 0 < remaining[0] <= min(
        PROBE_TIMEOUT_SECONDS, EXECUTION_LEASE_SECONDS - pause
    )
    assert all(deadline <= deadlines[0] for deadline in deadlines)


@pytest.mark.parametrize("pause", [0.0, 24.5])
def test_actual_http_wait_is_cut_off_by_remaining_lease(tmp_path: Path, pause: float) -> None:
    started: list[bool] = []
    completed: list[bool] = []

    async def delayed(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        started.append(True)
        await asyncio.sleep(1)
        completed.append(True)
        return successful_response(CheckKind.BASIC_TEXT)

    h = check_dispatch(tmp_path, handler=delayed)
    epoch: datetime = h.now
    origin = time.monotonic()
    offset = 0.0

    def now() -> datetime:
        return epoch + timedelta(seconds=offset + time.monotonic() - origin)

    h.clock = now
    h.worker = replace(h.worker, common=replace(h.common, now=now))
    original_begin = h.store.begin
    paused = False

    @contextmanager
    def begin() -> Iterator[object]:
        nonlocal paused, offset
        with original_begin() as db:
            yield db
        if not paused and h.store.checks[h.identifier].state is CheckState.RUNNING:
            paused = True
            offset = pause

    h.store.begin = begin
    process_connection_check(h.identifier, dependencies=h.worker)
    assert started == [True]
    if pause == 0:
        assert h.store.checks[h.identifier].state is CheckState.SUCCEEDED
        assert completed == [True]
    else:
        assert h.store.checks[h.identifier].state is CheckState.UNKNOWN
        assert completed == [], "HTTP completed after the committed lease expired"
