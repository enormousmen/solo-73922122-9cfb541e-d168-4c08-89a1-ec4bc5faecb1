# 园区电表区间用电量 API

把网关补传的电表**累计读数**转换成**区间用电量**，供异常耗电排查使用。

技术栈：FastAPI + SQLite（WAL），Docker Compose 启动两个 API 进程共享同一个
数据库卷。

## 快速开始

```bash
docker compose up -d --build
```

| 地址 | 用途 |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI / api-1（可执行入仓、撤销、查询） |
| <http://localhost:8000/redoc> | ReDoc |
| <http://localhost:8000/openapi.json> | OpenAPI 规范 |
| <http://localhost:8002/docs> | api-2，第二个进程，同一份数据 |
| <http://localhost:8000/health> | 健康检查（含当前提交序号） |

两个端口背后是**两个独立的 API 进程，共用 `/data/energy.sqlite3`**；可以交替
向两个端口发送写入以验证跨进程一致性（`scripts/e2e_compose.py` 就是这样做的）。

数据保存在命名卷 `energy-data` 中：`docker compose down` 保留数据，
`docker compose down -v` 清空重来。想加载演示数据（含乱序补传、换表、
计数回退样例）时设置 `ENERGY_SEED_DEMO=1` 后启动 api 容器，例如：

```bash
ENERGY_SEED_DEMO=1 docker compose up -d --build
curl -s localhost:8000/api/v1/meters | python3 -m json.tool
```

本地（非 Docker）运行：

```bash
python3 -m pip install -r requirements-dev.txt
ENERGY_DB_PATH=/tmp/energy.sqlite3 python3 -m app.run     # http://127.0.0.1:8000/docs
python3 -m pytest                                        # 含双进程并发/崩溃测试
```

## API

### 入仓 — `POST /api/v1/readings`

一次提交一批读数，**整批原子提交**（要么全部生效，要么整批拒绝）。

```json
{
  "readings": [
    {
      "meter_id": "M-1001",
      "source_record_id": "gw-0001",
      "read_at": "2026-10-01T00:00:00Z",
      "reading": 1000,
      "meter_replaced": false
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `meter_id` | 电表号 |
| `source_record_id` | 源记录号，全局唯一，用于重试去重与撤销 |
| `read_at` | UTC 时刻（ISO-8601）。带时区会换算为 UTC；不带时区按 UTC 解释 |
| `reading` | 累计读数，非负整数（度）；接受整数、整值浮点、数字字符串 |
| `meter_replaced` | 换表标记，缺省 `false`；接受 `true/false` 或 `1/0` |

返回：

```json
{
  "commit_seq": 3,
  "changed": true,
  "counts": {"created": 2},
  "records": [{"source_record_id": "gw-0001", "status": "created"}, ...],
  "affected_meters": [{"meter_id": "M-1001", "reading_count": 2,
                       "interval_count": 1, "break_count": 0,
                       "usage_total": 120, "first_read_at": "...", "last_read_at": "..."}]
}
```

`records[].status` 为 `created`（新写入）、`unchanged`（同号同内容重试，
不重复写、不推进提交序号）。整份重试时 `changed=false`、`counts` 全为
`unchanged`。

### 撤销 — `POST /api/v1/readings/undo`

```json
{"source_record_ids": ["gw-0001", "gw-0002"]}
```

按源记录号删除读数并重算这些电表的**全部区间**；不存在的号码返回
`not_found`（不算失败）；撤销后相邻读数会自动连成新的区间。

### 查询 — `GET /api/v1/meters/{meter_id}`

可选参数 `from` / `to`（ISO-8601，含端点）与 `include_intervals`。
响应来自**同一个数据库快照**：

```json
{
  "meter_id": "M-1001",
  "commit_seq": 5,
  "window": {"from": null, "to": null, "applied": false},
  "readings": [{"source_record_id": "...", "read_at": "...", "reading": 1000, ...}],
  "intervals": [{
      "from_source_record_id": "...", "to_source_record_id": "...",
      "from_read_at": "...", "to_read_at": "...",
      "from_reading": 1000, "to_reading": 1120,
      "usage": 120, "is_break": false, "break_reason": null, "commit_seq": 5 }],
  "breaks": [...],
  "usage_total": 120
}
```

`GET /api/v1/meters` 列出所有电表的汇总。无任何读数的电表返回 404。

### 匹配 — `POST /api/v1/meters/match`

**只读**接口：网关维护后部分电表换了上报编号，旧号与新号在交接时段同时
上传读数。该接口确认旧号与新号的一对一对应，避免数仓把同一电表算两次。

```json
{
  "old_meter_ids": ["M-1001", "M-1002"],
  "new_meter_ids": ["M-2001", "M-2002"],
  "window": {"from": "2026-10-01T00:00:00Z", "to": "2026-10-01T08:00:00Z"}
}
```

* 旧号、新号各 2～6 个、数量相同且全部互异；`window` 为最长 24 小时的
  UTC 重叠时段（含端点）。
* 比较**同一已提交快照**（响应带 `commit_seq`）中双方**同起止时刻**、
  完全落在重叠时段内的有效区间。
* 某对旧/新号成立的条件：存在**连续三段**共同有效区间用量相同，且这三段
  中**至少出现两种不同的正用量**；共同有效区间一旦出现用量冲突，该组合
  直接排除；断点或缺采样会打断连续证据，不能拼接。
* 仅当全局一对一对应**唯一**时返回 `status=matched`、全部配对及每段证据
  的源记录号；否则 `pairs` 为空并说明原因，不猜测部分配对：
  * `insufficient_evidence`：证据不足（某编号在窗口内没有有效区间，或
    与任何对方编号都凑不齐连续三段相同用量 / 存在用量冲突）；
  * `no_complete_matching`：候选配对拼不成覆盖全部编号的一对一对应；
  * `ambiguous`：存在多种一对一对应，无法唯一确定。

响应示例（唯一对应时）：

```json
{
  "status": "matched",
  "commit_seq": 7,
  "window": {"from": "2026-10-01T00:00:00Z", "to": "2026-10-01T08:00:00Z",
             "applied": true},
  "pairs": [{
    "old_meter_id": "M-1001",
    "new_meter_id": "M-2001",
    "evidence": [{
      "from_read_at": "...", "to_read_at": "...", "usage": 120,
      "old_from_source_record_id": "gw-0041",
      "old_to_source_record_id": "gw-0042",
      "new_from_source_record_id": "gw-1107",
      "new_to_source_record_id": "gw-1108"
    }]
  }],
  "reason": null
}
```

### 错误

| 状态码 | 含义 |
| --- | --- |
| 409 `batch_rejected` | 整批拒绝，`conflicts[]` 说明每条冲突 |
| 422 | 请求体/时间窗口参数不合法 |
| 404 `meter_not_found` | 该电表没有读数 |
| 503 `database_busy` | 等待写锁超时，可重试（带 `Retry-After`） |

## 规则与语义

* **区间 = 同表相邻两条读数之差。** 读数按 `(电表号, UTC 时刻)` 排序，
  无论到达顺序如何（乱序补传会把新读数插到中间并重算前后区间）。
* **断点**：以下两种情况记断点，该区间 `usage` 为 `null`，不推算用量：
  * 后一条读数带 `meter_replaced=true`（换表）；
  * 后一条读数小于前一条（计数回退）。
  断点区间的两端读数仍会被后续正常区间使用。
* **同号同内容重试**幂等：不写库、不推进提交序号、不重算。
* **同号异内容**、**同表同刻（不同源记录号）**：整批 409 拒绝，数据库保持
  原样（校验在写入之前完成）。
* 同一次请求里重复的源记录号：内容相同则合并；内容不同或同表同刻不同号则
  整批拒绝。
* 撤销后再提交同一源记录号视为全新记录。

## 一致性、并发与故障安全

* 共享库、两个进程：所有写操作在 `BEGIN IMMEDIATE` 事务里执行，SQLite 的
  写锁把并发写串行化（`busy_timeout=30s`），不会出现 SQLite 层面互相覆盖。
  `(source_record_id)` 与 `(meter_id, read_at_us)` 的唯一约束在库内兜底。
* 读数与区间在**同一个事务**内写入；每次变更后该电表的区间全部重建，
  因此区间表始终是读数表的函数。
* 查询在显式读事务（WAL 快照）中执行，读数、区间、断点、`usage_total`
  同属一次已提交状态；响应里的 `commit_seq` 标识该状态。
* 失败/重启：整笔提交在一个事务内，未提交即回滚，进程被杀不会留下半笔
  结果；启动时校验 `user_version`，测试另跑 `PRAGMA integrity_check` 与
  `foreign_key_check`。
* 区间表有 `CHECK` 约束：断点区间 `usage` 必须为 `NULL`，正常区间必须有值。

## 测试

```bash
python3 -m pytest            # 56 个用例
```

* `tests/test_api.py`：区间/断点/乱序/幂等/冲突/撤销/窗口/校验。
* `tests/test_matching.py`：旧新编号匹配的唯一对应、证据不足、无完整对应、
  多种对应、断点/缺采样打断连续证据、用量冲突、窗口与入参校验。
* `tests/test_concurrency.py`：启动两个真实进程共享同一个 SQLite 文件，
  并发写、并发重试、竞争冲突、持续读写一致性、进程崩溃（`os._exit`）、
  重启后数据延续、未提交事务回滚。
* `tests/test_durability.py`：区间重建中途异常时整批回滚、提交序号不前进、
  多进程同时初始化建库、schema 版本保护。

验证 Compose 部署：

```bash
python3 scripts/e2e_compose.py http://localhost:8000 http://localhost:8002
```

## 目录结构

```
app/            FastAPI 应用（main.py 路由、service.py 领域逻辑、db.py 存储）
tests/          功能与并发/故障测试
scripts/        Compose 部署冒烟脚本
Dockerfile, docker-compose.yml
```
