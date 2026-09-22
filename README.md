# SVG 轮廓规范化 API

基于 **FastAPI + SQLite + Docker** 的服务：接收 SVG `path` 数据与填充规则，
按轮廓之间的**包含层级**把每个子路径分类为**外环 / 孔洞（可嵌套出“岛”）**，
并重写绘制方向（外环顺时针、孔洞逆时针），同时保留轮廓层级、原始索引与自定义元数据，
对边界情况给出明确警告。启动后自动建表，可直接调用。

此外提供**版本差异审计**端点 `POST /paths/compare`：对两条已存路径分别规范化，
按轮廓几何指纹与原始索引稳定匹配，输出 `added` / `removed` / `modified` 三组差异。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由、模型、启动自动初始化
│   ├── db.py          # SQLite 连接与幂等建表
│   ├── compare.py     # 版本差异审计：指纹+索引稳定匹配与变化分类
│   └── svggeo.py      # SVG path 解析 / 细分 / 包含层级 / 方向重写 / 几何指纹（零三方依赖）
├── tests/             # pytest 单元 + API 端到端测试（44 个）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

服务在 <http://localhost:8000>，交互式文档在 <http://localhost:8000/docs>。
容器启动时通过 FastAPI lifespan 自动在 `/data/svgapi.db` 建表（数据存放在命名卷
`svgapi-data` 中），健康探针 `GET /healthz` 通过后即可调用。

不用 Compose 时：

```bash
docker build -t svg-contour-normalizer .
docker run -p 8000:8000 -v "$PWD/data:/data" svg-contour-normalizer
```

本地裸机运行：

```bash
pip install -r requirements.txt
SVGAPI_DB_PATH=./svgapi.db uvicorn app.main:app --reload
```

## 方向约定

采用 SVG 屏幕坐标系（**y 轴向下**）：

| 角色 | 层级深度 | 目标方向 | 有符号面积（鞋带公式） |
|------|----------|----------|------------------------|
| 外环 `outer` | 偶数（0, 2, 4 …） | 顺时针 | `> 0` |
| 孔洞 `hole`  | 奇数（1, 3, 5 …） | 逆时针 | `< 0` |

- 包含层级通过“多点采样 + 射线法点在多边形内 + 边界检测”投票判定，
  选择**最紧的包含轮廓**作为父轮廓；支持孔中岛、岛中孔的任意偶数层嵌套。
- `nonzero` 下与约定相反的原始方向会被重写并产生 `NONZERO_DIR_CONFLICT` 警告；
  `evenodd` 渲染本身不依赖方向，服务仍统一为外顺内逆，结果对两种填充规则均正确。
- 贝塞尔（`C/S/Q/T`）与椭圆弧（`A`）在几何分析时细分为折线；
  方向重写时**保留原始命令形态**（曲线只交换控制点顺序，弧只翻转 `sweep-flag`）。

## API

### `POST /paths` — 写入 path 与填充规则

请求体：

| 字段 | 类型 | 说明 |
|------|------|------|
| `d` | string | SVG path 数据（必填） |
| `fill_rule` | `nonzero` \| `evenodd` | 默认 `nonzero` |
| `metadata` | object | 路径级自定义元数据，原样保留 |
| `contour_metadata` | object[] | 按**轮廓原始索引**给出的自定义元数据 |

无法解析的 `d` 或非法填充规则返回 `422`，坏数据不会落库。

```bash
curl -s -X POST localhost:8000/paths -H 'Content-Type: application/json' -d '{
  "d": "M0,0h40v40h-40Z M10,10h20v20h-20Z",
  "fill_rule": "nonzero",
  "metadata": {"part": "法兰", "material": "Q235"},
  "contour_metadata": [{"name": "外法兰"}, {"name": "螺栓孔"}]
}'
# -> {"id":1, "d":"...", "fill_rule":"nonzero", "metadata":{...}, "contour_metadata":[...]}
```

### `POST /paths/{id}/normalize` — 分类并重写方向

幂等：重复调用结果一致。返回原始 `d`、规范化结果（新 `d`、每个轮廓的层级信息、
边界警告）。

```bash
curl -s -X POST localhost:8000/paths/1/normalize
```

响应中的 `normalized.contours[]`：

| 字段 | 说明 |
|------|------|
| `original_index` | 该轮廓在原始 `d` 中的子路径序号（从 0 开始） |
| `role` | `outer` / `hole` / `open` / `degenerate` |
| `depth` | 嵌套深度，0 为最外层；开放/退化轮廓为 `null` |
| `parent_original_index` | 直接父轮廓的原始索引（顶层为 `null`） |
| `closed` | 原始子路径是否以 `Z` 闭合 |
| `signed_area` | 细分折线的有符号面积 |
| `bounds` | `minx/miny/maxx/maxy` 包围盒 |
| `reversed` | 本次是否重写了方向 |
| `fingerprint` | 轮廓几何指纹（与方向、闭合环起点、原始索引无关） |
| `metadata` | 创建时按索引提交的自定义元数据 |

### `GET /paths/{id}` — 查询原始路径、规范化结果与警告

未规范化时 `normalized` 为 `null`；已规范化时包含 `d`、`contours`、
`warnings` 与方向约定说明。`id` 不存在返回 `404`。

```bash
curl -s localhost:8000/paths/1
```

### `POST /paths/compare` — 版本差异审计

对两条**已存路径**分别规范化（不改动库中已有规范化结果），按轮廓
**几何指纹 + 原始索引**建立稳定匹配，返回 `added`（候选侧新增）、
`removed`（基线侧删除）、`modified`（修改）三组结果。

请求体：

| 字段 | 类型 | 说明 |
|------|------|------|
| `base_id` | int（>0） | 基线（旧版本）路径 id |
| `candidate_id` | int（>0） | 候选（新版本）路径 id |

匹配规则：指纹相同且原始索引相同的轮廓优先配对；随后同指纹轮廓允许
**跨原始索引**配对——因此仅调换子路径顺序不算差异，相同指纹但索引变化
仍视为同一轮廓；剩余同索引、不同指纹的轮廓按“几何被改动”配对。
任一路径不存在返回 `404`；参数缺失/非法（如 `id<=0`、类型错误）或任一路径
无法规范化返回 `422`；同一 id 自比返回三组全空。

`modified[]` 的 `change_types` 区分三类变化（可同时出现，按
`geometry → hierarchy → direction` 固定顺序排列）：

| change_type | 判定 |
|-------------|------|
| `direction` | 仅原始绘制方向变化（规范化前有符号面积符号 `cw/ccw/zero` 不同），指纹一致 |
| `hierarchy` | `role` 或 `depth` 变化（如孔中“岛”删除中间层后降为孔洞） |
| `geometry` | 几何指纹不一致（形状被改动；可能同时伴随方向/层级变化） |

每个 `modified` 条目都携带**两侧**的 `original_index`、`role`、`depth`、
`metadata` 与 `fingerprint`，以及 `details.direction` / `details.hierarchy`
的前后对照；`added` / `removed` 条目为单侧的同名字段。

```bash
curl -s -X POST localhost:8000/paths/compare \
  -H 'Content-Type: application/json' \
  -d '{"base_id": 1, "candidate_id": 2}'
```

响应示例（删除中间层孔洞，原内“岛”降为孔洞，且轮廓原始索引由 2 变为 1；
指纹相同，跨索引匹配为同一轮廓，仅报层级变化）：

```json
{
  "base_id": 1,
  "candidate_id": 2,
  "added": [],
  "removed": [
    {"original_index": 1, "role": "hole", "depth": 1,
     "metadata": {"name": "中孔"}, "fingerprint": "10f5bd5b1ce45d74"}
  ],
  "modified": [
    {
      "change_types": ["hierarchy"],
      "base": {"original_index": 2, "role": "outer", "depth": 2,
               "metadata": {"name": "内岛"}, "fingerprint": "a924734add5e32fc"},
      "candidate": {"original_index": 1, "role": "hole", "depth": 1,
                    "metadata": {"name": "内岛改"}, "fingerprint": "a924734add5e32fc"},
      "details": {
        "direction": {"base": "cw", "candidate": "cw"},
        "hierarchy": {
          "base": {"role": "outer", "depth": 2},
          "candidate": {"role": "hole", "depth": 1}
        }
      }
    }
  ],
  "summary": {"added": 0, "removed": 1, "modified": 1, "unchanged": 1}
}
```

仅方向变化的例子（同一孔洞由顺时针改为逆时针）：

```bash
# base:      M0,0h40v40h-40Z M10,10h20v20h-20Z
# candidate: M0,0h40v40h-40Z M10,30h20v-20h-20Z
curl -s -X POST localhost:8000/paths/compare \
  -H 'Content-Type: application/json' \
  -d '{"base_id": 3, "candidate_id": 4}'
# -> modified[0].change_types == ["direction"]，role/depth 两侧均为 hole/1
```

几何指纹在规范化输出的每个 `contours[].fingerprint` 中也可直接查看：
它对闭合环的绘制方向与起点循环移位无关（开放路径仅与方向无关），
并对量化步长（1e-9）内的浮点噪声稳健。

### `GET /healthz` — 健康探针

返回 `{"status":"ok"}`，同时用于 Docker HEALTHCHECK。

## 警告码（`normalized.warnings[]`）

| code | 含义 |
|------|------|
| `EMPTY_PATH` | `d` 为空或没有子路径 |
| `OPEN_CONTOUR` | 子路径未以 `Z` 闭合，不参与层级判定，方向原样保留 |
| `DEGENERATE_CONTOUR` | 面积为 0 或顶点不足，跳过方向重写 |
| `NONZERO_DIR_CONFLICT` | `nonzero` 下原方向与外顺内逆约定相反，已重写 |
| `TOUCHING_PARENT_BOUNDARY` | 采样点落在候选父轮廓边上，包含关系可能不严格 |

每条警告带 `contour_original_index`（全局警告为 `null`）与中文 `message`。

## 支持的 path 语法

`M/m L/l H/h V/v C/c S/s Q/q T/t A/a Z/z`，包括：相对/绝对坐标、
隐式命令重复（多组坐标对）、`Z` 之后的隐式 moveto、标志位与数字紧贴
（如 `A5,5 0 00 10,0`）或逗号分隔（`0,1,1`）。弧线半径自动修正遵循
SVG 规范的端点参数化算法。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q          # 44 passed
```

覆盖：解析器边界、面积符号、外/孔/岛三层嵌套、同级双孔、曲线与弧反转几何等价性、
开放与退化轮廓、元数据保留、几何指纹（反向/循环移位不变性、抗浮点噪声、几何变化识别）、
完整 API 生命周期、404/422、重复规范化幂等，以及版本差异审计的
方向/层级/几何变化分类、同指纹跨索引匹配、纯重排无差异、added/removed、
元数据携带与参数校验。

## 数据存储

SQLite 单表 `paths`（WAL 模式）保存原始 `d`、填充规则、两类元数据、
规范化 `d`、规范化详情与警告 JSON。默认路径由环境变量 `SVGAPI_DB_PATH`
控制（容器内 `/data/svgapi.db`）。
