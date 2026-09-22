"""SVG 轮廓规范化 API。

端点：
    POST /paths               写入 SVG path 数据及填充规则
    POST /paths/{id}/normalize 按轮廓包含层级分类外环/孔洞并重写方向
    POST /paths/compare        两条已存路径的版本差异审计（指纹匹配 + 三类变更）
    GET  /paths/{id}          查询原始路径、规范化结果及边界警告
    GET  /healthz             存活/就绪探针
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from sqlite3 import Connection

from . import db
from .diff import compare_normalized
from .svggeo import SVGPathError, normalize_path


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()  # 自动初始化：启动即建表
    yield


app = FastAPI(
    title="SVG 轮廓规范化 API",
    version="1.1.0",
    description="写入 SVG path，按包含层级分类外环/孔洞并重写绕向，"
                "并支持两条已存路径的版本差异审计。",
    lifespan=lifespan,
)


def get_db() -> Connection:
    with db.get_conn() as conn:
        yield conn


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

class PathIn(BaseModel):
    d: str = Field(..., description="SVG path 数据，如 'M0,0h10v10h-10Z'")
    fill_rule: str = Field("nonzero", description="nonzero 或 evenodd")
    metadata: dict[str, Any] = Field(default_factory=dict,
                                    description="路径级自定义元数据，原样保留")
    contour_metadata: list[dict[str, Any]] = Field(
        default_factory=list,
        description="按轮廓原始索引提供的自定义元数据，规范化时挂到对应轮廓")


class CompareIn(BaseModel):
    base_id: int = Field(..., gt=0, description="基线路径 id")
    candidate_id: int = Field(..., gt=0, description="候选（新版本）路径 id")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "d": row["d"],
        "fill_rule": row["fill_rule"],
        "metadata": json.loads(row["metadata"] or "{}"),
        "contour_metadata": json.loads(row["contour_metadata"] or "[]"),
        "normalized": None
        if row["normalized_d"] is None
        else {
            "d": row["normalized_d"],
            **json.loads(row["normalized_info"]),
        },
        "created_at": row["created_at"],
    }


def _get_row(conn: Connection, path_id: int):
    row = conn.execute("SELECT * FROM paths WHERE id = ?", (path_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"路径 id={path_id} 不存在")
    return row


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/paths", status_code=status.HTTP_201_CREATED)
def create_path(payload: PathIn,
                conn: Connection = Depends(get_db)) -> dict[str, Any]:
    if payload.fill_rule not in ("nonzero", "evenodd"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="fill_rule 必须是 'nonzero' 或 'evenodd'")

    # 提前校验可解析性：坏数据不落库
    try:
        normalize_path(payload.d, payload.fill_rule)
    except SVGPathError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"SVG path 解析失败: {exc}")

    cur = conn.execute(
        "INSERT INTO paths (d, fill_rule, metadata, contour_metadata) "
        "VALUES (?, ?, ?, ?)",
        (payload.d, payload.fill_rule,
         json.dumps(payload.metadata, ensure_ascii=False),
         json.dumps(payload.contour_metadata, ensure_ascii=False)),
    )
    return {"id": cur.lastrowid, "d": payload.d,
            "fill_rule": payload.fill_rule,
            "metadata": payload.metadata,
            "contour_metadata": payload.contour_metadata}


def _normalize_row(row) -> dict[str, Any]:
    """对已落库行即时规范化（仅用于审计计算，不写库）。"""
    contour_meta = json.loads(row["contour_metadata"] or "[]")
    return normalize_path(
        row["d"], row["fill_rule"],
        contour_metadata=contour_meta if contour_meta else None,
    )


@app.post("/paths/compare")
def compare_paths(payload: CompareIn,
                  conn: Connection = Depends(get_db)) -> dict[str, Any]:
    # 两路均须存在：先报告基线缺失，任一路径不存在返回 404
    base_row = conn.execute(
        "SELECT * FROM paths WHERE id = ?", (payload.base_id,)).fetchone()
    if base_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"base_id={payload.base_id} 路径不存在")
    cand_row = conn.execute(
        "SELECT * FROM paths WHERE id = ?", (payload.candidate_id,)).fetchone()
    if cand_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"candidate_id={payload.candidate_id} 路径不存在")

    # 已存路径通常在写入时已校验，此处防御性处理损坏/历史脏数据 -> 422
    try:
        base_norm = _normalize_row(base_row)
    except SVGPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"base_id={payload.base_id} 路径无法规范化: {exc}")
    try:
        cand_norm = _normalize_row(cand_row)
    except SVGPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"candidate_id={payload.candidate_id} 路径无法规范化: {exc}")

    result = compare_normalized(base_norm, cand_norm)
    return {"base_id": payload.base_id,
            "candidate_id": payload.candidate_id,
            **result}


@app.post("/paths/{path_id}/normalize")
def normalize(path_id: int,
              conn: Connection = Depends(get_db)) -> dict[str, Any]:
    row = _get_row(conn, path_id)
    try:
        result = _normalize_row(row)
    except SVGPathError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"SVG path 规范化失败: {exc}")

    normalized_d = result.pop("normalized_d")
    conn.execute(
        "UPDATE paths SET normalized_d = ?, normalized_info = ?, warnings = ? "
        "WHERE id = ?",
        (normalized_d, json.dumps(result, ensure_ascii=False),
         json.dumps(result["warnings"], ensure_ascii=False), path_id),
    )

    return {
        "id": path_id,
        "d": row["d"],
        "fill_rule": row["fill_rule"],
        "normalized": {"d": normalized_d, **result},
    }


@app.get("/paths/{path_id}")
def get_path(path_id: int, conn: Connection = Depends(get_db)) -> dict[str, Any]:
    return _row_to_dict(_get_row(conn, path_id))
