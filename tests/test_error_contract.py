from control_plane.app.bootstrap.app import create_app


def test_openapi_declares_problem_responses_for_all_existing_operations() -> None:
    schema = create_app().openapi()

    assert "Problem" in schema["components"]["schemas"]
    problem_properties = schema["components"]["schemas"]["Problem"]["properties"]
    assert set(problem_properties) == {"type", "title", "status", "detail", "requestId"}
    assert problem_properties["requestId"]["type"] == "string"

    health = schema["paths"]["/healthz"]["get"]["responses"]
    ready = schema["paths"]["/readyz"]["get"]["responses"]
    me = schema["paths"]["/api/v1/me"]["get"]["responses"]
    navigation = schema["paths"]["/api/v1/navigation"]["get"]["responses"]

    assert "200" in ready and "503" in ready
    ready_schema_ref = ready["200"]["content"]["application/json"]["schema"]["$ref"]
    ready_schema_name = ready_schema_ref.rsplit("/", maxsplit=1)[1]
    ready_status = schema["components"]["schemas"][ready_schema_name]["properties"]["status"]
    assert ready_status["const"] == "ready"
    for responses in (health, ready, me, navigation):
        assert "500" in responses
    for responses in (me, navigation):
        assert "401" in responses and "403" in responses
    assert ready["503"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/Problem"
    }
