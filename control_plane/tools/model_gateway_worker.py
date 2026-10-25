"""Run one bounded check batch; never retries a RUNNING or terminal probe."""

import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict

from control_plane.app.bootstrap.model_gateway_worker import (
    model_check_worker_dependencies,
    model_metadata_worker_dependencies,
)
from control_plane.app.modules.model_gateway import (
    ModelCheckWorkerDependencies,
    ModelMetadataWorkerDependencies,
    run_model_check_batch,
    run_model_metadata_batch,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    dependencies_provider: Callable[
        [], ModelCheckWorkerDependencies
    ] = model_check_worker_dependencies,
    metadata_dependencies_provider: Callable[
        [], ModelMetadataWorkerDependencies
    ] = model_metadata_worker_dependencies,
) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded Model Gateway batch")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--kind",
        choices=("connection-checks", "metadata-observations"),
        default="connection-checks",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100:
        print(json.dumps({"errorCode": "INVALID_ARGUMENT"}))
        return 2
    try:
        result = (
            run_model_metadata_batch(
                dependencies=metadata_dependencies_provider(), limit=args.limit
            )
            if args.kind == "metadata-observations"
            else run_model_check_batch(dependencies=dependencies_provider(), limit=args.limit)
        )
    except Exception:
        print(
            json.dumps(
                {
                    "errorCode": "MODEL_METADATA_WORKER_UNAVAILABLE"
                    if args.kind == "metadata-observations"
                    else "MODEL_CHECK_WORKER_UNAVAILABLE"
                }
            )
        )
        return 1
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
