from control_plane.app.bootstrap.app import create_app


def test_default_control_plane_exposes_only_the_v03_requirement_slice() -> None:
    paths = set(create_app().openapi()["paths"])

    assert {
        "/api/v1/requirements",
        "/api/v1/requirements/{requirementId}",
    } <= paths
    assert {
        "/api/v1/requirements/{requirementId}/sdd-baselines",
        "/api/v1/requirements/{requirementId}/baseline-confirmations",
        "/api/v1/requirements/{requirementId}/baseline-decisions",
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:start",
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:request-integration-mr",
        "/api/v1/requirements/{requirementId}/work-items/{workItemId}:request-integration-merge",
    }.isdisjoint(paths)

    collection = create_app().openapi()["paths"]["/api/v1/requirements"]
    assert {"get", "post"} <= set(collection)
    assert "get" in create_app().openapi()["paths"]["/api/v1/requirements/{requirementId}"]


def test_v03_work_item_contract_does_not_publish_delivery_fields() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    properties = schemas["WorkItemResponseDto"]["properties"]

    assert {
        "integrationDeliveryState",
        "integrationMergeRequestBindingId",
        "integrationBlockedReasonCode",
        "integrationUpdatedAt",
    }.isdisjoint(properties)
