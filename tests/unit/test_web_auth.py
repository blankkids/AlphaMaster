from __future__ import annotations

from fastapi.testclient import TestClient

import web.app as web_app
from web.auth import create_session_token, verify_session_token


def configure_auth(monkeypatch) -> None:
    monkeypatch.setenv("WEB_AUTH_USERNAME", "operator")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("WEB_AUTH_SECRET", "test-session-secret")
    monkeypatch.setenv("WEB_AUTH_SESSION_HOURS", "12")


def test_anonymous_requests_require_login(monkeypatch) -> None:
    configure_auth(monkeypatch)
    client = TestClient(web_app.app)

    page_response = client.get("/", follow_redirects=False)
    api_response = client.get("/api/routes")

    assert page_response.status_code == 303
    assert page_response.headers["location"] == "/login?next=%2F"
    assert api_response.status_code == 401
    assert "登录" in api_response.json()["detail"]
    assert client.get("/login").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_login_grants_access_and_logout_revokes_it(monkeypatch) -> None:
    configure_auth(monkeypatch)
    client = TestClient(web_app.app)

    wrong_response = client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "wrong"},
    )
    login_response = client.post(
        "/api/auth/login",
        json={
            "username": "operator",
            "password": "correct-horse-battery-staple",
        },
    )

    assert wrong_response.status_code == 401
    assert login_response.status_code == 200
    cookie = login_response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert client.get("/api/routes").status_code == 200
    assert client.get("/").status_code == 200

    logout_response = client.post("/api/auth/logout")

    assert logout_response.status_code == 200
    assert client.get("/api/routes").status_code == 401


def test_unconfigured_password_denies_login(monkeypatch) -> None:
    monkeypatch.setenv("WEB_AUTH_USERNAME", "operator")
    monkeypatch.delenv("WEB_AUTH_PASSWORD", raising=False)
    client = TestClient(web_app.app)

    status_response = client.get("/api/auth/status")
    login_response = client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "anything"},
    )

    assert status_response.status_code == 200
    assert status_response.json()["configured"] is False
    assert status_response.json()["login_username"] == "operator"
    assert login_response.status_code == 503


def test_session_token_rejects_expiry_and_tampering(monkeypatch) -> None:
    configure_auth(monkeypatch)
    token = create_session_token("operator", now=1_000)

    assert verify_session_token(token, now=1_001) == "operator"
    assert verify_session_token(token, now=50_000) is None

    payload, signature = token.split(".", 1)
    replacement = "A" if signature[-1] != "A" else "B"
    tampered = f"{payload}.{signature[:-1]}{replacement}"
    assert verify_session_token(tampered, now=1_001) is None
