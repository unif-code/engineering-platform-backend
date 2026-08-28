import argparse
import json
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass

from sqlalchemy.exc import SQLAlchemyError

from control_plane.app.bootstrap.source_control_runtime import (
    SourceControlRuntime,
    source_control_runtime_context,
)
from control_plane.app.modules.source_control import (
    RequirementCallbackUnavailable,
    SourceControlDependencies,
    SourceControlDependencyUnavailable,
    process_due_source_control_inboxes,
    reconcile_due_source_control_effects,
    relay_due_source_control_requests,
)

_COMMAND_MINIMUM_LIMITS = {"relay": 2, "process": 3, "reconcile": 2}


@dataclass(frozen=True, slots=True)
class WorkerRunReport:
    command: str
    claimed: int
    processed: int
    released: int = 0
    effect_ids: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()


def run_worker_once(
    command: str,
    *,
    limit: int,
    dependencies: SourceControlDependencies,
) -> WorkerRunReport:
    facades = {
        "relay": relay_due_source_control_requests,
        "process": process_due_source_control_inboxes,
        "reconcile": reconcile_due_source_control_effects,
    }
    try:
        facade = facades[command]
    except KeyError:
        raise ValueError("unsupported worker command") from None
    result = facade(limit=limit, dependencies=dependencies)
    return WorkerRunReport(
        command=command,
        claimed=result.claimed,
        processed=result.processed,
        released=result.released,
        effect_ids=result.effect_ids,
        error_codes=result.error_codes,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Source Control worker batch")
    parser.add_argument("command", choices=("relay", "process", "reconcile"))
    parser.add_argument("--limit", type=int, default=50)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    dependencies_provider: Callable[[], SourceControlDependencies] | None = None,
    runtime_context_provider: Callable[
        [], AbstractContextManager[SourceControlRuntime]
    ] = source_control_runtime_context,
) -> int:
    args = _parser().parse_args(argv)
    if args.limit < _COMMAND_MINIMUM_LIMITS[args.command]:
        print(json.dumps({"command": args.command, "errorCodes": ["INVALID_ARGUMENT"]}))
        return 2
    try:
        if dependencies_provider is not None:
            report = run_worker_once(
                args.command,
                limit=args.limit,
                dependencies=dependencies_provider(),
            )
        else:
            with runtime_context_provider() as runtime:
                report = run_worker_once(
                    args.command,
                    limit=args.limit,
                    dependencies=runtime.dependencies,
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
