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


def test_compare_identical_is_empty(client):
    pid = _mk(client, "M0,0h40v40h-40Z M10,10h20v20h-20Z")
    r = client.post("/paths/compare", json={"base_id": pid, "candidate_id": pid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == [] and body["removed"] == [] and body["modified"] == []
    assert body["summary"] == {
        "base_contour_count": 2, "candidate_contour_count": 2,
        "added": 0, "removed": 0, "modified": 0, "unchanged": 2}


def test_compare_orientation_only_change(client):
    base = _mk(client, "M0,0h40v40h-40Z M10,10h20v20h-20Z")
    # 内孔同一几何、逆时针书写
    cand = _mk(client, "M0,0h40v40h-40Z M10,30h20v-20h-20Z")
    body = client.post("/paths/compare",
                       json={"base_id": base, "candidate_id": cand}).json()
    assert len(body["modified"]) == 1
    m = body["modified"][0]
    assert m["change_type"] == "orientation"
    assert m["matched_by"] == "fingerprint"
    assert m["fingerprint"]["base"] == m["fingerprint"]["candidate"]
    assert m["base"]["original_index"] == 1
    assert m["candidate"]["original_index"] == 1
    assert m["base"]["role"] == m["candidate"]["role"] == "hole"
    assert m["base"]["depth"] == m["candidate"]["depth"] == 1
    assert m["base"]["metadata"] == {}


def test_compare_hierarchy_change_takes_priority_over_orientation(client):
    # 基线：大外环 + 内孔（孔顺时针，面积为正）
    base = _mk(client, "M0,0h100v100h-100Z M10,10h20v20h-20Z")
    # 候选：大外环消失，同一小环成为顶层外环（仍顺时针）-> 层级变化
    cand = _mk(client, "M10,10h20v20h-20Z")
    body = client.post("/paths/compare",
                       json={"base_id": base, "candidate_id": cand}).json()
    assert len(body["removed"]) == 1
    assert body["removed"][0]["base_original_index"] == 0
    assert body["removed"][0]["base_role"] == "outer"
    assert len(body["modified"]) == 1
    m = body["modified"][0]
    assert m["change_type"] == "hierarchy"
    assert m["matched_by"] == "fingerprint"
    assert m["base"]["role"] == "hole" and m["base"]["depth"] == 1
    assert m["candidate"]["role"] == "outer" and m["candidate"]["depth"] == 0


def test_compare_geometry_change_matched_by_original_index(client):
    base = _mk(client, "M0,0h40v40h-40Z M10,10h20v20h-20Z")
    cand = _mk(client, "M0,0h40v40h-40Z M12,12h16v16h-16Z")
    body = client.post("/paths/compare",
                       json={"base_id": base, "candidate_id": cand}).json()
    assert len(body["modified"]) == 1
    m = body["modified"][0]
    assert m["change_type"] == "geometry"
    assert m["matched_by"] == "index"
    assert m["fingerprint"]["base"] != m["fingerprint"]["candidate"]
    assert m["base"]["original_index"] == m["candidate"]["original_index"] == 1
    assert m["base"]["role"] == m["candidate"]["role"] == "hole"


def test_compare_same_fingerprint_but_index_changed_is_unchanged(client):
    # 子路径顺序重排：相同指纹、原始索引互换，仍视为同一轮廓
    base = _mk(client, "M0,0h40v40h-40Z M10,10h20v20h-20Z")
    cand = _mk(client, "M10,10h20v20h-20Z M0,0h40v40h-40Z")
    body = client.post("/paths/compare",
                       json={"base_id": base, "candidate_id": cand}).json()
    assert body["modified"] == []
    assert body["added"] == [] and body["removed"] == []
    assert body["summary"]["unchanged"] == 2


def test_compare_added_and_removed(client):
    base = _mk(client, "M0,0h40v40h-40Z M10,10h20v20h-20Z")
    cand = _mk(client,
               "M0,0h40v40h-40Z M30,30h5v5h-5Z M10,10h20v20h-20Z")
    body = client.post("/paths/compare",
                       json={"base_id": base, "candidate_id": cand}).json()
    # 原有两轮廓按指纹匹配为 unchanged，新增一个孔；新增项按候选索引升序
    assert body["summary"] == {
        "base_contour_count": 2, "candidate_contour_count": 3,
        "added": 1, "removed": 0, "modified": 0, "unchanged": 2}
    added = body["added"][0]
    assert added["candidate_original_index"] == 1
    assert added["candidate_role"] == "hole"
    assert added["candidate_depth"] == 1
    assert len(added["candidate_fingerprint"]) == 64

    # 反向比较：同一条轮廓变为 removed
    rev = client.post("/paths/compare",
                      json={"base_id": cand, "candidate_id": base}).json()
    assert rev["removed"][0]["base_original_index"] == 1
    assert rev["summary"]["removed"] == 1 and rev["summary"]["added"] == 0


def test_compare_identical_duplicate_contours_pair_stably(client):
    # 两条坐标完全相同的孔（同指纹、不同索引）：新增第三条同形孔时只计一个 added
    hole = "M10,10h10v10h-10Z"
    base = _mk(client, f"M0,0h100v100h-100Z {hole} {hole}")
    cand = _mk(client, f"M0,0h100v100h-100Z {hole} {hole} {hole}")
    body = client.post("/paths/compare",
                       json={"base_id": base, "candidate_id": cand}).json()
    assert body["summary"]["added"] == 1
    assert body["summary"]["unchanged"] == 3
    assert body["modified"] == [] and body["removed"] == []

    # 重排后（孔与外环交换位置）仍全部识别为同一轮廓
    reordered = _mk(client, f"{hole} M0,0h100v100h-100Z {hole}")
    body = client.post("/paths/compare",
                       json={"base_id": base, "candidate_id": reordered}).json()
    assert body["summary"]["unchanged"] == 3
    assert body["added"] == [] and body["removed"] == [] and body["modified"] == []


def test_compare_carries_metadata_both_sides(client):
    base = _mk(client, "M0,0h40v40h-40Z M10,10h20v20h-20Z",
               contour_metadata=[{"name": "壳体-v1"}, {"name": "轴孔-v1"}])
    cand = _mk(client, "M0,0h40v40h-40Z M12,12h16v16h-16Z",
               contour_metadata=[{"name": "壳体-v2"}, {"name": "轴孔-v2"}])
    body = client.post("/paths/compare",
                       json={"base_id": base, "candidate_id": cand}).json()
    m = body["modified"][0]
    assert m["base"]["metadata"] == {"name": "轴孔-v1"}
    assert m["candidate"]["metadata"] == {"name": "轴孔-v2"}
    # 未变化的轮廓不作为 modified 返回
    assert len(body["modified"]) == 1


def test_compare_does_not_require_prior_normalization(client):
    # 写入后不调用 /normalize，compare 仍可即时规范化
    pid = _mk(client, "M0,0h10v10h-10Z")
    assert client.get(f"/paths/{pid}").json()["normalized"] is None
    r = client.post("/paths/compare", json={"base_id": pid, "candidate_id": pid})
    assert r.status_code == 200
    # 审计不落库、不产生 normalized 结果
    assert client.get(f"/paths/{pid}").json()["normalized"] is None


def test_compare_404_when_either_path_missing(client):
    pid = _mk(client, "M0,0h10v10h-10Z")
    r = client.post("/paths/compare", json={"base_id": 999999, "candidate_id": pid})
    assert r.status_code == 404
    assert "base_id" in r.json()["detail"]
    r = client.post("/paths/compare", json={"base_id": pid, "candidate_id": 999999})
    assert r.status_code == 404
    assert "candidate_id" in r.json()["detail"]


def test_compare_422_on_invalid_params(client):
    pid = _mk(client, "M0,0h10v10h-10Z")
    assert client.post("/paths/compare",
                       json={"base_id": 0, "candidate_id": pid}).status_code == 422
    assert client.post("/paths/compare",
                       json={"candidate_id": pid}).status_code == 422
    assert client.post("/paths/compare",
                       json={"base_id": "x", "candidate_id": pid}).status_code == 422


def test_compare_422_when_stored_path_unparseable(client):
    # 直接落库一条无法解析的历史脏数据
    from app import db
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO paths (d, fill_rule, metadata, contour_metadata) "
            "VALUES (?, 'nonzero', '{}', '[]')",
            ("M0,0 L",))
        bad_id = cur.lastrowid
    good = _mk(client, "M0,0h10v10h-10Z")
    r = client.post("/paths/compare", json={"base_id": bad_id, "candidate_id": good})
    assert r.status_code == 422
    assert "base_id" in r.json()["detail"]
    r = client.post("/paths/compare", json={"base_id": good, "candidate_id": bad_id})
    assert r.status_code == 422
    assert "candidate_id" in r.json()["detail"]
