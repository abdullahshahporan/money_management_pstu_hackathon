"""The interactive API docs must send authentication, not only display a field."""

from apps.api.main import create_app


def test_swagger_uses_http_bearer_security_scheme() -> None:
    schema = create_app().openapi()

    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "description": (
            "Paste the accessToken returned by /auth/login. "
            "Swagger adds 'Bearer ' automatically."
        ),
        "scheme": "bearer",
    }
    account_operation = schema["paths"]["/api/v1/accounts/me"]["get"]
    assert {"BearerAuth": []} in account_operation["security"]
    assert all(
        parameter.get("name", "").lower() != "authorization"
        for parameter in account_operation.get("parameters", [])
    )
