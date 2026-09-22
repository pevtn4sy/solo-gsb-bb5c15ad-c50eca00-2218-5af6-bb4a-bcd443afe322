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


# ---------------------------------------------------------------------------
# POST /paths/compare 版本差异审计
# ---------------------------------------------------------------------------

def _mk(client, d, **kw):
    return client.post("/paths", json={"d": d, **kw}).json()["id"]


def _cmp(client, base_id, candidate_id):
    return client.post("/paths/compare",
                       json={"base_id": base_id,
                             "candidate_id": candidate_id})


def test_compare_direction_only(client):
    # 同一几何孔洞，仅绘制方向由顺时针改为逆时针
    bid = _mk(client, "M0,0h40v40h-40Z M10,10h20v20h-20Z")
    cid = _mk(client, "M0,0h40v40h-40Z M10,30h20v-20h-20Z")
    r = _cmp(client, bid, cid)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["added"] == [] and data["removed"] == []
    assert len(data["modified"]) == 1
    m = data["modified"][0]
    assert m["change_types"] == ["direction"]
    assert m["base"]["original_index"] == 1
    assert m["candidate"]["original_index"] == 1
    assert m["base"]["role"] == m["candidate"]["role"] == "hole"
    assert m["base"]["depth"] == m["candidate"]["depth"] == 1
    assert m["details"]["direction"] == {"base": "cw", "candidate": "ccw"}
    assert data["summary"] == {"added": 0, "removed": 0,
                               "modified": 1, "unchanged": 1}


def test_compare_hierarchy_only_with_index_shift(client):
    # 删除中间层孔洞后，最内“岛”降为孔洞：同指纹跨索引匹配，仅层级变化
    bid = _mk(client,
              "M0,0h60v60h-60Z M5,5h50v50h-50Z M10,10h40v40h-40Z")
    cid = _mk(client, "M0,0h60v60h-60Z M10,10h40v40h-40Z")
    r = _cmp(client, bid, cid)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["removed"]) == 1
    assert data["removed"][0]["original_index"] == 1
    assert data["added"] == []
    assert len(data["modified"]) == 1
    m = data["modified"][0]
    assert m["change_types"] == ["hierarchy"]
    # 相同指纹但原始索引从 2 变为 1，仍视为同一轮廓
    assert m["base"]["original_index"] == 2
    assert m["candidate"]["original_index"] == 1
    assert m["base"]["fingerprint"] == m["candidate"]["fingerprint"]
    assert (m["base"]["role"], m["base"]["depth"]) == ("outer", 2)
    assert (m["candidate"]["role"], m["candidate"]["depth"]) == ("hole", 1)


def test_compare_geometry_change(client):
    bid = _mk(client, "M0,0h40v40h-40Z")
    cid = _mk(client, "M0,0h50v50h-50Z")
    data = _cmp(client, bid, cid).json()
    assert len(data["modified"]) == 1
    m = data["modified"][0]
    assert m["change_types"] == ["geometry"]
    assert m["base"]["fingerprint"] != m["candidate"]["fingerprint"]
    # 外/孔角色与深度不变
    assert m["base"]["role"] == m["candidate"]["role"] == "outer"
    assert m["base"]["depth"] == m["candidate"]["depth"] == 0


def test_compare_geometry_and_direction_together(client):
    # 几何不同且方向相反：两类变化同时报告
    bid = _mk(client, "M0,0h40v40h-40Z")
    cid = _mk(client, "M0,50 L50,50 L50,0 L0,0 Z")
    data = _cmp(client, bid, cid).json()
    m = data["modified"][0]
    assert set(m["change_types"]) == {"geometry", "direction"}
    assert m["details"]["direction"] == {"base": "cw", "candidate": "ccw"}


def test_compare_added_and_removed(client):
    bid = _mk(client, "M0,0h10v10h-10Z")
    cid = _mk(client, "M0,0h10v10h-10Z M20,20h10v10h-10Z")
    data = _cmp(client, bid, cid).json()
    assert data["removed"] == []
    assert len(data["added"]) == 1
    assert data["added"][0]["original_index"] == 1
    assert data["added"][0]["role"] == "outer"
    assert data["added"][0]["depth"] == 0

    # 反向审计：新增变删除
    rev = _cmp(client, cid, bid).json()
    assert rev["added"] == []
    assert rev["removed"][0]["original_index"] == 1


def test_compare_reorder_is_unchanged(client):
    # 两条互不包含的外轮廓调换子路径顺序：指纹相同，不算任何差异
    bid = _mk(client, "M0,0h10v10h-10Z M50,50h10v10h-10Z")
    cid = _mk(client, "M50,50h10v10h-10Z M0,0h10v10h-10Z")
    data = _cmp(client, bid, cid).json()
    assert data["added"] == []
    assert data["removed"] == []
    assert data["modified"] == []
    assert data["summary"]["unchanged"] == 2


def test_compare_carries_metadata(client):
    bid = _mk(client,
              "M0,0h40v40h-40Z M10,10h20v20h-20Z",
              contour_metadata=[{"name": "壳体"}, {"name": "轴孔"}])
    cid = _mk(client,
              "M0,0h40v40h-40Z M10,30h20v-20h-20Z",
              contour_metadata=[{"name": "壳体"}, {"name": "轴孔v2"}])
    m = _cmp(client, bid, cid).json()["modified"][0]
    assert m["base"]["metadata"] == {"name": "轴孔"}
    assert m["candidate"]["metadata"] == {"name": "轴孔v2"}


def test_compare_same_id_is_empty(client):
    pid = _mk(client, "M0,0h40v40h-40Z M10,10h20v20h-20Z")
    data = _cmp(client, pid, pid).json()
    assert data["added"] == [] and data["removed"] == []
    assert data["modified"] == []
    assert data["summary"]["unchanged"] == 2


def test_compare_404(client):
    pid = _mk(client, "M0,0h10v10h-10Z")
    assert _cmp(client, 999999, pid).status_code == 404
    assert _cmp(client, pid, 999999).status_code == 404


def test_compare_422_invalid_params(client):
    pid = _mk(client, "M0,0h10v10h-10Z")
    assert client.post("/paths/compare",
                       json={"candidate_id": pid}).status_code == 422
    assert _cmp(client, 0, pid).status_code == 422
    assert client.post("/paths/compare",
                       json={"base_id": "x",
                             "candidate_id": pid}).status_code == 422


def test_compare_unnormalizable_raises():
    # 直接覆盖审计层：路径无法规范化时抛出 SVGPathError（路由映射为 422）
    import pytest
    from app.compare import compare_paths
    from app.svggeo import SVGPathError

    with pytest.raises(SVGPathError):
        compare_paths("M0,0 L", "nonzero", None,
                      "M0,0h10Z", "nonzero", None)
