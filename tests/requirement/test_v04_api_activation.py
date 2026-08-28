from control_plane.app.bootstrap.app import create_app


def test_default_control_plane_exposes_exact_v04_requirement_slice() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]

    expected = {
        "/api/v1/requirements",
        "/api/v1/requirements/{requirementId}",
        "/api/v1/requirements/{requirementId}/sdd-artifacts",
        (
            "/api/v1/requirements/{requirementId}/sdd-artifacts/"
            "{artifactId}/versions/{artifactVersion}"
        ),
        "/api/v1/requirements/{requirementId}/work-items",
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:assign",
        "/api/v1/requirements/{requirementId}/sdd-baselines",
        "/api/v1/requirements/{requirementId}/baseline-confirmations",
        "/api/v1/requirements/{requirementId}/baseline-gates/{gateId}:reassign",
        "/api/v1/requirements/{requirementId}/baseline-decisions",
    }
    assert expected <= set(paths)
    assert {
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:start",
        ("/api/v1/requirements/{requirementId}/work-items/{workItemId}:request-integration-mr"),
        ("/api/v1/requirements/{requirementId}/work-items/{workItemId}:request-integration-merge"),
    }.isdisjoint(paths)

    assert {"get", "post"} <= set(paths["/api/v1/requirements"])
    assert "get" in paths["/api/v1/requirements/{requirementId}"]
    assert (
        "get"
        in paths[
            "/api/v1/requirements/{requirementId}/sdd-artifacts/"
            "{artifactId}/versions/{artifactVersion}"
        ]
    )


def test_v04_mutations_publish_strict_concurrency_and_security_contract() -> None:
    paths = create_app().openapi()["paths"]
    mutations = {
        "/api/v1/requirements/{requirementId}/sdd-artifacts": ("Requirement", "201"),
        "/api/v1/requirements/{requirementId}/work-items": ("Requirement", "201"),
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:assign": (
            "WorkItem",
            "200",
        ),
        "/api/v1/requirements/{requirementId}/sdd-baselines": ("Requirement", "201"),
        "/api/v1/requirements/{requirementId}/baseline-confirmations": (
            "Requirement",
            "201",
        ),
        "/api/v1/requirements/{requirementId}/baseline-gates/{gateId}:reassign": (
            "Gate",
            "200",
        ),
        "/api/v1/requirements/{requirementId}/baseline-decisions": ("Requirement", "200"),
    }

    for path, (aggregate, status) in mutations.items():
        operation = paths[path]["post"]
        headers = {item["name"]: item for item in operation["parameters"] if item["in"] == "header"}
        assert headers["Idempotency-Key"]["required"] is True
        assert headers["If-Match"]["required"] is True
        assert operation["security"] == [{"EpSessionCookie": []}]
        response = operation["responses"][status]
        assert aggregate in response["headers"]["ETag"]["description"]


def test_v04_details_publish_route_planning_and_gate_metadata_without_delivery_fields() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    requirement = schemas["RequirementResponseDto"]["properties"]
    details = schemas["RequirementDetailsResponseDto"]["properties"]
    work_item = schemas["WorkItemResponseDto"]["properties"]
    gate = schemas["GateInstanceResponseDto"]["properties"]

    assert "routeSnapshot" in requirement
    assert {
        "workItemAssignments",
        "currentSddBaseline",
        "currentGate",
        "currentGateAssignment",
        "currentDecision",
    } <= set(details)
    assert {"policyCode", "policySnapshotHash"} <= set(gate)
    assert {
        "integrationDeliveryState",
        "integrationMergeRequestBindingId",
        "integrationBlockedReasonCode",
        "integrationUpdatedAt",
    }.isdisjoint(work_item)
