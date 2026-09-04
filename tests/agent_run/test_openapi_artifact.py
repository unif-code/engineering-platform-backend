import json

from control_plane.app import __version__
from control_plane.app.bootstrap.app import create_app
from control_plane.app.bootstrap.sandbox_controller import create_sandbox_controller_app
from scripts.export_openapi import render, render_sandbox


def test_public_and_private_openapi_are_deterministic_versioned_and_disjoint() -> None:
    public_first, public_second = render(), render()
    private_first, private_second = render_sandbox(), render_sandbox()
    public = json.loads(public_first)
    private = json.loads(private_first)
    prefix = "/api/v1/internal/sandbox"

    assert public_first == public_second
    assert private_first == private_second
    assert public["info"]["version"] == private["info"]["version"] == __version__
    assert public == create_app().openapi()
    assert private == create_sandbox_controller_app().openapi()
    assert not any(path.startswith(prefix) for path in public["paths"])
    assert set(private["paths"]) == {
        f"{prefix}/materializations",
        f"{prefix}/materializations/{{materialization_id}}",
        f"{prefix}/materializations/{{materialization_id}}/preview",
        f"{prefix}/materializations/{{materialization_id}}/checkpoint-release",
        f"{prefix}/materializations/{{materialization_id}}/handoff",
        f"{prefix}/materializations/{{materialization_id}}/finalize",
        f"{prefix}/executions/{{execution_id}}/cancel",
        f"{prefix}/leases/reconcile",
    }


def test_private_openapi_has_workload_security_headers_and_no_physical_contract() -> None:
    schema = json.loads(render_sandbox())

    assert schema["components"]["securitySchemes"]["WorkloadBearer"] == {
        "scheme": "bearer",
        "type": "http",
    }
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post"}:
                continue
            assert operation["security"] == [{"WorkloadBearer": []}]
            success = next(
                response
                for status, response in operation["responses"].items()
                if status in {"200", "201"}
            )
            assert "ETag" in success["headers"]
            parameters = {item["name"]: item for item in operation.get("parameters", [])}
            if method == "post":
                assert parameters["Idempotency-Key"]["required"] is True
            if any(
                path.endswith(suffix)
                for suffix in ("/preview", "/checkpoint-release", "/handoff", "/finalize")
            ):
                assert parameters["If-Match"]["required"] is True

    serialized = render_sandbox().lower()
    for forbidden in (
        "api_key",
        "credential",
        "kata",
        "kubernetes",
        "pod",
        "provider",
        "region",
        "runtimeclass",
        "secret_value",
    ):
        assert forbidden not in serialized
    assert "**********" not in serialized
