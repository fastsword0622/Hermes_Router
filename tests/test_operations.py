import json
import logging
from pathlib import Path
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from fastapi.testclient import TestClient
from urllib.error import HTTPError
from hermes_router.api import create_app
from hermes_router.client import request_json
from hermes_router.config import Settings
from hermes_router.operations import administrator_token, observed_metrics, rotating_log
from hermes_router.redaction import register_secrets
from hermes_router.development import DevelopmentStore


def test_authorization_required_except_health(client):
    token = client.headers.pop("Authorization")
    assert client.get("/health").status_code == 200
    assert client.get("/v1/jobs").status_code == 401
    assert client.post("/v1/jobs", json={"prompt": "x", "repository": "/repo"}).status_code == 401
    assert client.get("/v1/jobs", headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    client.headers["Authorization"] = token
    assert client.get("/v1/jobs").status_code == 200


def test_token_is_persistent_owner_only_and_not_in_responses(tmp_path):
    settings = Settings(data_dir=tmp_path)
    first = administrator_token(settings)
    assert administrator_token(settings) == first
    assert stat.S_IMODE((tmp_path / "admin-token").stat().st_mode) == 0o600
    with TestClient(create_app(settings)) as c:
        c.headers["Authorization"] = "Bearer " + first
        response = c.post("/v1/jobs", json={"prompt": "x", "repository": "/repo"})
        assert response.status_code == 201
        assert first not in response.text
    (tmp_path / "admin-token").chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        administrator_token(settings)


def test_request_limit_including_chunked_body(tmp_path):
    settings = Settings(data_dir=tmp_path, api_token="operations-test-admin-token", max_request_bytes=64)
    with TestClient(create_app(settings)) as c:
        c.headers["Authorization"] = "Bearer " + settings.api_token
        assert c.post("/v1/jobs", content=b"a" * 100).status_code == 413
        assert c.post("/v1/jobs", content=iter([b"a"*40, b"b"*40])).status_code == 413


def test_validation_does_not_echo_credentials(client):
    secret = "sk-" + "a" * 40
    response = client.post("/v1/jobs", json={"prompt": "x", "repository": "/repo", "mode": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert "input" not in response.json()["detail"][0]


def test_rotating_logs_redact_known_values_and_keep_private_modes(tmp_path):
    settings = Settings(data_dir=tmp_path, log_max_bytes=300, log_backup_count=2)
    settings.ensure_dirs()
    secret = "opaque-private-registered-value"
    register_secrets([secret])
    handler = rotating_log(settings)
    logger = logging.getLogger("hermes_router.operations-test")
    try:
        for _ in range(30):
            logger.warning("credential output %s", secret)
        handler.flush()
    finally:
        logging.getLogger("hermes_router").removeHandler(handler)
        handler.close()
    paths = list(settings.log_dir.glob("router.log*"))
    assert len(paths) == 3
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in paths)
    assert all(secret not in p.read_text() for p in paths)


def test_metrics_are_observed_and_unknown_is_not_zero(tmp_path):
    store = DevelopmentStore(tmp_path / "jobs.db")
    job = store.create("x", "/repo", "mock")
    store.mutate(job["id"], lambda j: j.update(status="succeeded", attempts=[
        {"provider": "minimax", "number": 1, "error": "none", "verified": False, "duration_seconds": 1,
         "verification": [{"passed": False}]},
        {"provider": "glm", "number": 2, "error": "none", "verified": True, "duration_seconds": 2,
         "verification": [{"passed": True}]}]))
    result = observed_metrics(store, "mock")
    assert result["providers"]["minimax"]["success_rate"] == 0
    assert result["providers"]["glm"]["success_rate"] == 1
    assert result["verification"]["pass_rate"] == 0.5
    assert result["cost"] is None and result["tokens"] is None
    assert observed_metrics(store, "hermes")["jobs"] == 0
    assert stat.S_IMODE((tmp_path / "jobs.db").stat().st_mode) == 0o600
    store.close()


def test_operator_token_is_never_forwarded_through_http_redirect(tmp_path):
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/collect")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = tmp_path / "admin-token"
    token.write_text("redirect-test-local-only-secret")
    try:
        with pytest.raises(HTTPError) as error:
            request_json(f"http://127.0.0.1:{server.server_port}", "GET", "/health", token_path=token)
        assert error.value.code == 302
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
