import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.bootstrap.source_control_connector import source_control_runtime_engine
from control_plane.app.modules.source_control import (
    RequirementCallbackUnavailable,
    SourceControlDependencies,
    SourceControlDependencyUnavailable,
    process_binding_request,
    process_webhook_inbox,
    reconcile_due_effects,
    relay_binding_requests,
)


@dataclass(frozen=True, slots=True)
class WorkerRunReport:
    command: str
    claimed: int
    processed: int
    released: int = 0
    effect_ids: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()


def source_control_worker_dependencies() -> SourceControlDependencies:
    # Provider credentials and cross-module runtime adapters arrive with the
    # later GitOps assembly. Until then the executable is explicitly fail-closed.
    source_control_runtime_engine()
    raise SourceControlDependencyUnavailable("Source Control worker dependencies are unavailable")


def _run_process(*, limit: int, dependencies: SourceControlDependencies) -> WorkerRunReport:
    now = dependencies.clock.now()
    with dependencies.engine.connect() as db:
        repository = dependencies.repository_factory(db)
        message_ids = repository.pending_binding_request_ids(limit=limit, now=now)
        webhook_ids = repository.pending_webhook_ids(limit=max(limit - len(message_ids), 0))
    effect_ids: list[str] = []
    error_codes: list[str] = []
    processed = 0
    for message_id in message_ids:
        result = process_binding_request(message_id=message_id, dependencies=dependencies)
        processed += 1
        if result.effect is not None:
            effect_ids.append(result.effect.id)
        if result.blocked_reason is not None:
            error_codes.append(result.blocked_reason)
    for inbox_id in webhook_ids:
        process_webhook_inbox(inbox_id=inbox_id, dependencies=dependencies)
        processed += 1
    return WorkerRunReport(
        command="process",
        claimed=len(message_ids) + len(webhook_ids),
        processed=processed,
        effect_ids=tuple(effect_ids),
        error_codes=tuple(error_codes),
    )


def run_worker_once(
    command: str,
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> WorkerRunReport:
    if limit < 1:
        raise ValueError("limit must be positive")
    if command == "relay":
        relay_result = relay_binding_requests(limit=limit, dependencies=dependencies)
        return WorkerRunReport(
            command=command,
            claimed=relay_result.claimed,
            processed=relay_result.accepted,
            released=relay_result.released,
        )
    if command == "process":
        return _run_process(limit=limit, dependencies=dependencies)
    if command == "reconcile":
        reconciliation = reconcile_due_effects(limit=limit, dependencies=dependencies)
        return WorkerRunReport(
            command=command,
            claimed=len(reconciliation.effects),
            processed=len(reconciliation.effects),
            effect_ids=tuple(effect.id for effect in reconciliation.effects),
            error_codes=tuple(
                effect.last_error_code
                for effect in reconciliation.effects
                if effect.last_error_code is not None
            ),
        )
    raise ValueError("unsupported worker command")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Source Control worker batch")
    parser.add_argument("command", choices=("relay", "process", "reconcile"))
    parser.add_argument("--limit", type=int, default=50)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    dependencies_provider: Callable[
        [], SourceControlDependencies
    ] = source_control_worker_dependencies,
) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_worker_once(
            args.command,
            limit=args.limit,
            dependencies=dependencies_provider(),
        )
    except (
        RequirementCallbackUnavailable,
        SourceControlDependencyUnavailable,
        SQLAlchemyError,
    ):
        print(json.dumps({"command": args.command, "errorCodes": ["DEPENDENCY_UNAVAILABLE"]}))
        return 1
    except ValueError:
        print(json.dumps({"command": args.command, "errorCodes": ["INVALID_ARGUMENT"]}))
        return 2
    print(json.dumps(asdict(report), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
