"""API 端到端测试（FastAPI TestClient + 临时 SQLite）。"""

import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["SVGAPI_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_full_lifecycle(client):
    body = {
        "d": "M0,0h40v40h-40Z M10,10h20v20h-20Z",
        "fill_rule": "nonzero",
        "metadata": {"part": "法兰", "material": "Q235"},
        "contour_metadata": [{"name": "外法兰"}, {"name": "螺栓孔"}],
    }
    r = client.post("/paths", json=body)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["metadata"]["part"] == "法兰"

    r = client.post(f"/paths/{pid}/normalize")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["normalized"]["fill_rule"] == "nonzero"
    contours = data["normalized"]["contours"]
    assert contours[0]["role"] == "outer"
    assert contours[1]["role"] == "hole"
    assert contours[0]["metadata"] == {"name": "外法兰"}
    assert contours[1]["bounds"]["minx"] == 10

    r = client.get(f"/paths/{pid}")
    assert r.status_code == 200
    got = r.json()
    assert got["d"] == body["d"]
    assert got["normalized"]["d"] == data["normalized"]["d"]
    assert isinstance(got["normalized"]["warnings"], list)
    assert got["metadata"]["material"] == "Q235"


def test_get_unormalized_has_null(client):
    r = client.post("/paths", json={"d": "M0,0h10v10h-10Z"})
    pid = r.json()["id"]
    r = client.get(f"/paths/{pid}")
    assert r.status_code == 200
    assert r.json()["normalized"] is None


def test_404(client):
    assert client.get("/paths/999999").status_code == 404
    assert client.post("/paths/999999/normalize").status_code == 404


def test_invalid_d_rejected(client):
    r = client.post("/paths", json={"d": "M0,0 L", "fill_rule": "nonzero"})
    assert r.status_code == 422


def test_invalid_fill_rule_rejected(client):
    r = client.post("/paths",
                    json={"d": "M0,0h10Z", "fill_rule": "winding"})
    assert r.status_code == 422


def test_open_contour_warning_surfaced(client):
    r = client.post("/paths", json={"d": "M0,0 L40,0 L40,40"})
    pid = r.json()["id"]
    r = client.post(f"/paths/{pid}/normalize")
    warnings = r.json()["normalized"]["warnings"]
    assert any(w["code"] == "OPEN_CONTOUR" for w in warnings)


def test_repeated_normalize_idempotent(client):
    r = client.post("/paths",
                    json={"d": "M0,0h40v40h-40Z M10,10h20v20h-20Z"})
    pid = r.json()["id"]
    d1 = client.post(f"/paths/{pid}/normalize").json()["normalized"]["d"]
    d2 = client.post(f"/paths/{pid}/normalize").json()["normalized"]["d"]
    assert d1 == d2
