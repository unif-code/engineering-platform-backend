from control_plane.app.bootstrap.app import create_app


def test_protected_operations_declare_cookie_security_and_problem_responses() -> None:
    schema = create_app().openapi()
    assert schema["components"]["securitySchemes"]["EpSessionCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "ep_session",
    }

    protected_operation_ids = {
        "identity_me",
        "identity_navigation",
        "grants_list",
        "grants_create",
        "grants_revoke",
        "org_tree",
        "org_set_superior",
        "workspace_list",
        "workspace_create",
        "workspace_invite_leader",
        "workspace_remove_leader",
        "workspace_transfer_owner",
        "workspace_members",
    }
    operations = {
        operation["operationId"]: operation
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "delete", "patch"}
    }
    assert protected_operation_ids <= operations.keys()
    for operation_id in protected_operation_ids:
        operation = operations[operation_id]
        assert operation["security"] == [{"EpSessionCookie": []}]
        assert {"401", "403", "503"} <= operation["responses"].keys()
        for status in ("401", "403", "503"):
            problem = operation["responses"][status]
            assert "application/problem+json" in problem["content"]

    operation_ids = [
        operation["operationId"]
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "delete", "patch"}
    ]
    assert len(operation_ids) == len(set(operation_ids))
