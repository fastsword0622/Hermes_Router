import json
from uuid import uuid4
import pytest
from hermes_router import client


@pytest.mark.parametrize("command,method,suffix", [("status", "GET", ""), ("start", "POST", "/start"),
    ("cancel", "POST", "/cancel"), ("report", "GET", "/report"), ("events", "GET", "/events"),
    ("history", "GET", "/report"), ("failures", "GET", "/report")])
def test_client_commands(monkeypatch, command, method, suffix, capsys):
    calls = []
    def request(*args):
        calls.append(args)
        return {"status": "failed", "attempts": [], "switches": []}
    monkeypatch.setattr(client, "request_json", request)
    job_id = str(uuid4())
    assert client.main([command, job_id]) == 0
    assert calls[0][1:3] == (method, "/v1/jobs/" + job_id + suffix)
    assert json.loads(capsys.readouterr().out)


def test_client_request_file_preserves_shell_metacharacters(tmp_path, monkeypatch):
    path = tmp_path / "request.txt"
    text = "$(touch should-not-execute); `whoami`\nquoted 'text'"
    path.write_text(text)
    calls = []
    monkeypatch.setattr(client, "request_json", lambda *args: calls.append(args) or {"id": "job"})
    assert client.main(["create", "--request-file", str(path), "--repository", "/repo"]) == 0
    assert calls[0][3]["prompt"] == text


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com", "http://user:pass@localhost", "file:///etc/passwd"])
def test_client_never_sends_operator_token_off_machine(url):
    with pytest.raises(ValueError, match="local HTTP"):
        client.request_json(url, "GET", "/health")
