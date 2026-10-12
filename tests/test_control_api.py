from types import SimpleNamespace

from fastapi.testclient import TestClient

from devtools.api import create_app
from ournotes.control.operations import OperationStore


def test_machine_session_auth_version_and_explicit_command_identity(tmp_path):
    store = OperationStore(tmp_path)
    calls = []
    service = SimpleNamespace(root=tmp_path, operations=store, running=lambda: False, status=lambda: {"version": "a" * 64}, index=SimpleNamespace(snapshot=SimpleNamespace(accounts=[])), execute=lambda action, params: calls.append((action, params)) or {"status": "accepted"}, operation=store.get)
    app = create_app(service, "session-secret-" + "x" * 32)
    with TestClient(app) as client:
        assert client.get("/api/v1/status").status_code == 401
        client.headers["Authorization"] = "Bearer session-secret-" + "x" * 32
        assert client.get("/api/v1/status").json()["version"] == "a" * 64
        body = {"action": "pause", "batch_id": "b" * 32}
        assert client.post("/api/v1/commands", json=body).status_code == 422
        body.update(command_id="pause-explicit-001", expected_version="a" * 64)
        assert client.post("/api/v1/commands", json=body).status_code == 200
        assert len(calls) == 1
        assert calls[0][1]["command_id"] == "pause-explicit-001"
        body["action"] = "shell"
        assert client.post("/api/v1/commands", json=body).status_code == 422
