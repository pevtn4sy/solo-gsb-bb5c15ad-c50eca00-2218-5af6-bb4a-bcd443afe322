"""两条已存 SVG 路径的版本差异审计。

先分别规范化两侧路径，再按**轮廓几何指纹 + 原始索引**建立稳定匹配，
输出 added / removed / modified 三组结果。

匹配策略（与几何无关的顺序重排不算差异）：
    1. 指纹相同且原始索引相同的轮廓优先配对；
    2. 两侧剩余轮廓按原始索引升序逐一尝试与“同指纹”的对侧轮廓配对
       （因此相同指纹但索引变化仍视为同一轮廓）；
    3. 其余同索引轮廓跨指纹配对（即几何被改动）；
    4. 最终未配对的候选侧轮廓计入 added，基线侧计入 removed。

modified 的变化类型：
    - ``direction``：仅原始绘制方向（有符号面积符号）变化；
    - ``hierarchy``：role 或 depth（外/孔层级）变化；
    - ``geometry``：指纹不一致（几何形状变化）。
三种类型可同时出现，按固定顺序列入 ``change_types``。
"""

from __future__ import annotations

from typing import Any

from .svggeo import normalize_path

_DIRECTION_TAGS = {"geometry": 0, "hierarchy": 1, "direction": 2}


def _contours_of(d: str, fill_rule: str,
                 contour_metadata: list[dict[str, Any]] | None
                 ) -> list[dict[str, Any]]:
    """规范化路径并取出轮廓审计视图（含指纹与原始方向）。"""
    result = normalize_path(
        d, fill_rule,
        contour_metadata=contour_metadata if contour_metadata else None,
    )
    return result["contours"]


def _pair_contours(base: list[dict[str, Any]],
                   candidate: list[dict[str, Any]]
                   ) -> list[tuple[dict[str, Any] | None,
                                   dict[str, Any] | None]]:
    """返回 ``(base_contour|None, candidate_contour|None)`` 配对列表。"""
    pairs: list[tuple[dict[str, Any] | None,
                      dict[str, Any] | None]] = []
    b_used: set[int] = set()
    c_used: set[int] = set()

    def by_index(items: list[dict[str, Any]]) -> dict[int, int]:
        return {item["original_index"]: pos
                for pos, item in enumerate(items)}

    b_idx, c_idx = by_index(base), by_index(candidate)

    def pair(bpos: int, cpos: int) -> None:
        b_used.add(bpos)
        c_used.add(cpos)
        pairs.append((base[bpos], candidate[cpos]))

    # 阶段 1：指纹 + 原始索引同时相同（最稳定的匹配）
    for oi in sorted(set(b_idx) & set(c_idx)):
        b, c = base[b_idx[oi]], candidate[c_idx[oi]]
        if b["fingerprint"] == c["fingerprint"]:
            pair(b_idx[oi], c_idx[oi])

    # 阶段 2：同指纹、允许原始索引变化（轮廓整体重排）
    for bpos in sorted((p for p in range(len(base)) if p not in b_used),
                       key=lambda p: base[p]["original_index"]):
        fp = base[bpos]["fingerprint"]
        cands = [p for p in range(len(candidate))
                 if p not in c_used and candidate[p]["fingerprint"] == fp]
        if cands:
            pair(bpos, min(cands,
                           key=lambda p: candidate[p]["original_index"]))

    # 阶段 3：同索引但指纹不同（同位置几何被改动）
    for oi in sorted(set(b_idx) & set(c_idx)):
        bp, cp = b_idx[oi], c_idx[oi]
        if bp not in b_used and cp not in c_used:
            pair(bp, cp)

    # 未配对项
    for cpos, c in enumerate(candidate):
        if cpos not in c_used:
            pairs.append((None, c))
    for bpos, b in enumerate(base):
        if bpos not in b_used:
            pairs.append((b, None))
    return pairs


def _orientation_tag(c: dict[str, Any]) -> str:
    """原始绘制方向（规范化已统一方向，故按有符号面积符号判断）。"""
    area = c["signed_area"]
    if area > 0:
        return "cw"
    if area < 0:
        return "ccw"
    return "zero"


def _classify(b: dict[str, Any],
              c: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    if b["fingerprint"] != c["fingerprint"]:
        changes.append("geometry")
    if b["role"] != c["role"] or b["depth"] != c["depth"]:
        changes.append("hierarchy")
    if _orientation_tag(b) != _orientation_tag(c):
        changes.append("direction")
    changes.sort(key=lambda t: _DIRECTION_TAGS[t])
    return changes


def _side_entry(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "original_index": c["original_index"],
        "role": c["role"],
        "depth": c["depth"],
        "metadata": c["metadata"],
        "fingerprint": c["fingerprint"],
    }


def compare_paths(base_d: str,
                  base_fill_rule: str,
                  base_contour_metadata: list[dict[str, Any]] | None,
                  candidate_d: str,
                  candidate_fill_rule: str,
                  candidate_contour_metadata: list[dict[str, Any]] | None
                  ) -> dict[str, Any]:
    """审计两条路径的轮廓级版本差异。

    任一 ``d`` 无法规范化时，由底层 ``normalize_path`` 抛出
    :class:`~app.svggeo.SVGPathError`，由路由层转换为 422。
    """
    base = _contours_of(base_d, base_fill_rule, base_contour_metadata)
    candidate = _contours_of(candidate_d, candidate_fill_rule,
                             candidate_contour_metadata)

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    unchanged = 0

    for b, c in _pair_contours(base, candidate):
        if b is None and c is not None:
            added.append(_side_entry(c))
        elif c is None and b is not None:
            removed.append(_side_entry(b))
        elif b is not None and c is not None:
            change_types = _classify(b, c)
            if not change_types:
                unchanged += 1  # 完全一致（含仅顺序重排）
                continue
            modified.append({
                "change_types": change_types,
                "base": _side_entry(b),
                "candidate": _side_entry(c),
                "details": {
                    "direction": {
                        "base": _orientation_tag(b),
                        "candidate": _orientation_tag(c),
                    },
                    "hierarchy": {
                        "base": {"role": b["role"], "depth": b["depth"]},
                        "candidate": {"role": c["role"],
                                      "depth": c["depth"]},
                    },
                },
            })

    added.sort(key=lambda e: e["original_index"])
    removed.sort(key=lambda e: e["original_index"])
    modified.sort(key=lambda e: (e["base"]["original_index"],
                                 e["candidate"]["original_index"]))

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "unchanged": unchanged,
        },
    }
