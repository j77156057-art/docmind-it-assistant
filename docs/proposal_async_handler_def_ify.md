# 实现方案预研：异步路由 Handler `def` 化（架构债 #1）

> 状态：已实施并推送（main@`d2339ef`，CI run `35781968760` success）；§9.1 为 2026-09-23 的决策 C 实测补充
> 作者：交付总监（team lead）
> 范围：DocMind IT Assistant — `app.py` 与 `admin_app.py` 的路由 handler 并发纪律
> 决策依据：架构评审（main@ca9db6f）债 #1「异步/线程池纪律不一致」

---

## 1. 背景与问题

FastAPI/Starlette 中，`async def` 路由 handler 如果在函数体内**直接调用阻塞式同步 DB 驱动**（本项目即同步 SQLAlchemy `Engine`），该调用会在**事件循环线程**里同步阻塞，导致同一进程内的所有其他请求被串行化——事件循环被这一个 handler 卡住，直到 DB 返回。

DocMind 的现状：

- `QueryDatabase` 是**同步 SQLAlchemy `Engine`**（`backend/database.py:63` `__init__` 接收 `url_or_path`，用 `pool_size` / `max_overflow` / `pool_timeout` 等同步 `create_engine` 参数构造；`database.py:88` `self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)`）。
- `app.py` / `admin_app.py` 中大量 `async def` 路由直接调用 `database.*`（如 `database.history()`、`database.usage_ledger()`、`database.list_documents()`、`database.set_document_acl()` 等），且 handler 体内**没有 `await`**。
- 这些 handler 在并发下会把事件循环占死，使"可水平扩展"目标在进程内就结构性失效。

架构师原话（债 #1）：`/api/history`、`/api/usage_summary`、`/api/usage/ledger`、`/api/admin/documents`、`document_acl`、`replace_document_acl` 等 `async def` 直接调阻塞 `QueryDatabase`；而 `/api/query`、`/api/feedback` 与重发布/导入路径已用 `run_in_threadpool`。**经代码核查，实际范围远大于这 6 个例子**（详见 §3）。

---

## 2. 修复原理（为什么 `def` 化是对的）

FastAPI 对 `def`（同步）路由 handler 会经 `starlette.concurrency.run_in_threadpool` → `anyio.to_thread.run_sync` **自动 offload 到工作线程**执行，事件循环得以继续处理其他请求。这正是对"同步 DB 驱动"的官方推荐做法。

> **机制措辞更正（2026-09-23 实测）**：这条路径**不是** `concurrent.futures.ThreadPoolExecutor`。本项目 anyio **4.15.1** 的 `to_thread` 使用自带的 `WorkerThread` 池，并发上限由默认 `CapacityLimiter` 决定，令牌数恒为 **40**；`ThreadPoolExecutor` 的默认 `min(32, cpu+4)`（本机 32）与本路径无关。实测数据见 §9.1 —— **"上限 40、且不是 32"是结论性的，不要再按 32 推算容量。**

安全性已逐条核实，无假设：

1. **`QueryDatabase` 线程安全**：每个公开方法都用 `with self._sessions() as session:`（或 `with self._sessions.begin() as session:`）**每次调用新建并关闭一个独立 Session**（`database.py` 全文件约 100 处 `with self._sessions...`）。无跨调用/跨线程共享的 Session 或连接状态。底层 `Engine` 本身线程安全。
2. **依赖均为同步**：`authenticated`（`app.py:135` `def`）、`viewer`/`auditor`（包装 `require_role`→`authenticated`，均为 `def`）、`require_capability`（admin 侧，同步 `Depends`）。`def` handler 不需要 `await` 任何依赖。
3. **契约不变**：FastAPI 对 `def` 与 `async def` 路由在签名、响应模型、OpenAPI 上完全等价，无接口变化。

结论：把"无 `await`、直接调阻塞 `database.*`"的 `async def` 路由改为 `def`，即可把阻塞调用移出事件循环，**零语义变化、线程安全**。

---

## 3. 适用范围（精确界定，基于代码核查）

### 3.1 可机械 `def` 化（无 `await`、无异步文件流）

**`app.py`（5 处）**——全部无 `await`：
- `app.py:393` `/api/history`
- `app.py:400` `/api/me`（不触 DB，但统一风格一并改）
- `app.py:410` `/api/runtime/model`（调 `models.status()`，同步）
- `app.py:414` `/api/usage/summary`
- `app.py:421` `/api/usage/ledger`

> 注：`app.py:340` `/api/query` 与 `app.py:379` `/api/feedback` **已是 `def`**，本就合规，不动。

**`admin_app.py`（约 40+ 处）**——所有"无 `await` 且直接调 `database.*`"的路由，例如：
- `admin_app.py:527` `documents`（`database.list_documents()`）
- `admin_app.py:557` `document_source`（`database.list_documents()` + `FileResponse`，无 `await`）
- `admin_app.py:586` `document_acl`（`database.document_access()`）
- `admin_app.py:594` `replace_document_acl`（`database.set_document_acl()`）
- `admin_app.py:609` `governance_pending`、`615` `version_reviews`、`625` `version_preview`、`645` `review_version`、`755` `withdraw_version`、`781` `rollback_version`
- `admin_app.py:807` `ingestion_jobs`、`825` `retry_ingestion_job`、`845` `cancel_ingestion_job`
- `admin_app.py:865` `evaluation_cases`、`881` `save_evaluation_case`、`908` `remove_evaluation_case`、`960` `evaluation_runs`、`972` `evaluation_run_detail`
- `admin_app.py:982` `audit_events`、`987` `audit_events_export`
- `admin_app.py:1079` `admin_citations`、`1086` `admin_citations_export`、`1099` `admin_feedback`、`1106` `admin_feedback_export`
- `admin_app.py:1118` `admin_knowledge_gaps`、`1125` `admin_knowledge_gaps_export`、`1138` `admin_resolve_knowledge_gap`、`1152` `admin_dismiss_knowledge_gap`
- `admin_app.py:1164` `admin_org_users`、`1170` `admin_org_groups`、`1176` `admin_org_departments`、`1182/1194/1206` 三个 `*_export`
- `admin_app.py:1219` `admin_org_department_create`、`1236` `delete`、`1251` `add_member`、`1267` `remove_member`
- `admin_app.py:1282` `retention_preview`、`1305` `retention_purge`
- `admin_app.py:1333` `metrics`、`1469` `download_artifact`（`FileResponse`，无 `await`）

（完整清单以 Grep `async def` + 函数体无 `await` 为机械判定依据，上述为代表性枚举。）

### 3.2 需逐 handler 重构（含真异步，不能直接 `def`）

- **`admin_app.py:1482` `import_document`**：函数体含 `await file.read()`（1510）、`await file.close()`（1612）与多处 `await run_in_threadpool(...)`（1515/1526/1553/1565）。涉及 `UploadFile` 异步文件流，不能机械 `def` 化。
  - 推荐改法（a，风险最低）：**保留 `async def`**，确保其中所有 `database.*` / 重发布调用已用 `run_in_threadpool` 包裹（已部分用，补齐遗漏即可），文件读维持 `await file.read()`。
  - 备选改法（b）：改 `def` 并把 `await file.read()` 换成同步 `file.file.read()`（`SpooledTemporaryFile` 同步读）、`await file.close()` 换 `file.file.close()`。侵入更大，不优先。

### 3.3 已正确 offload，本就不阻塞事件循环（保持现状即可）

以下 handler 已经用 `await run_in_threadpool(...)` 把阻塞调用移出事件循环，无需改动：
- `admin_app.py:682` `publish_version`（701）、`928` `create_evaluation_run`（938）、`1342` `model_config`（1344/1402）、`1437` `list_artifacts`（1438）、`1441` `create_artifact`（1446）、`import_document` 内 1515/1526/1553/1565。

> 若追求风格统一，也可把它们改为 `def` 并去掉 `await run_in_threadpool(...)` 包裹（直接调用）——属整洁性优化，**非必需**，见 §7 决策点 A。

### 3.4 必须保持 `async`（不转）

- **中间件**：`app.py:161` `request_logging`（`await call_next`）、`admin_app.py:285` `request_boundary`（`await call_next`）——是请求管线而非路由，必须 async。
- **异常处理器**：`app.py:128` `http_error`、`admin_app.py:236` `http_error`——不触 DB，无收益。
- **登录 / OIDC 回调类**（`oidc_start`/`oidc_callback`/`auth_login`/`auth_guest`/`auth_logout` 等）：若内部 `await` 外部 HTTP（`auth_transport` = `httpx` 异步传输）则必须保持 async。**本预研未逐一展开这些 handler 的 `await` 分布，列为实施前必核项**（见 §4 步骤 4）。

---

## 4. 实施步骤（机械、低风险）

1. Grep 锁定所有 `async def` 路由：排除 `@application.middleware(...)` 与 `@application.exception_handler(...)` 装饰的；剩余即路由 handler 候选。
2. 对每处候选：确认函数体**无 `await`**、无需要 async 生成的 `StreamingResponse` → 将 `async def` 改为 `def`。
3. `import_document`（1482）：按 §3.2 改法 (a) 处理，补齐 `run_in_threadpool` 包裹。
4. 登录 / OIDC 回调类 handler：逐一确认是否含 `await auth_transport` 调用；含则保留 `async def`，不含则一并 `def` 化。（本步决定最终改动清单大小。）
5. **不改动**依赖、`QueryDatabase`、`database.py`、中间件、异常处理器。
6. 单 commit：如 `refactor: offload blocking DB calls from event loop by def-ifying route handlers`。

---

## 5. 风险与缓解

| 风险 | 评估 | 缓解 |
|---|---|---|
| 误转含 `await` 的 handler | **极低**：`def` 内出现 `await` 是语法错误，根本无法启动/导入，CI + 本地 `pytest` 立即捕获 | 步骤 2 的"无 `await`"判定 + 全量 `pytest` 自校验 |
| 线程池饱和 | 中：并发上限是 anyio 默认 `CapacityLimiter` 的 **40** 个令牌（**不是** `ThreadPoolExecutor` 的 32，也没有 `max_workers` 这样的配置项）；`database` 连接池默认 `pool_size=5`/`max_overflow=10` → 40 个线程同时查 DB 时**连接池先饱和**，多出的至多 25 个线程排队到 `pool_timeout=30s` | 相比"事件循环被完全卡死"，这是有界、可观测的（已有 `slow_db_ms` 日志）。**约束链：线程上限(40) > 池容量(15) ⇒ 池才是真瓶颈**，要调就先调池。高 QPS 读面可显式调大 `database_pool_size` / `database_max_overflow`（follow-up，不阻塞本次；实测与判据见 §9.1） |
| OpenAPI / 行为变化 | 无：`def` 与 `async def` 路由对 FastAPI 等价 | 无需改动契约文档 |
| 中间件 / 异常处理器误转 | 已显式排除（§3.4） | 步骤 1 排除装饰器 |

---

## 6. 验证计划

- **基线回归**：`pytest` 全量（当前 232 passed / 4 skipped）——`def` 路由对 `TestClient` 透明，应仍全绿。
- **扩展并发证据**（直接证明债 #1 关闭）：在 `tests/test_stress_concurrency.py`（现有 A-5 压力测试，目前仅只读端点、不过 SQLAlchemy）新增一例，对**写/DB 路由**做并发压测：
  - 选 `/api/history`、`/api/usage/ledger`（app 侧）与 admin `documents` / `document_acl`（admin 侧）；
  - `concurrency ≥ 16`，带真实连接池（SQLite 或 PG）；
  - 断言全部返回 200，且**总耗时显著低于"串行耗时 × 并发数"**（即事件循环未被单请求占死）。
- **静态护栏（可选）**：新增 Grep 断言"路由 handler 内不得同时出现 `await` 与直接 `database.` 调用"，纳入 CI。

---

## 7. 需拍板的决策点

- **A. 风格统一度**：仅修"直接调 `database.*` 且无 `await`"的那批（最小、最安全），还是连"已用 `run_in_threadpool` 的 handler"也改成 `def` 并去 `await` 包裹（更统一，但 diff 更大）？
- **B. `import_document` 走法**：(a) 保 `async` + 补齐 `run_in_threadpool` 包裹（推荐，风险最低），还是 (b) 改 `def` + 同步文件读？
- **C. 连接池/线程池**：本次是否一并调大 `database_pool_size` / Starlette 线程池以防饱和，还是先改代码、观察 `slow_db_ms` 慢查询日志再定？
  - **2026-09-23 实测补充**：维持"先改代码、观察再定"。且本地已证伪"只调线程侧"——把 anyio 令牌从 40 调到 64/96，真实并行度确实抬到 64/96，但吞吐 109.4→105.8→99.5 req/s **不升反降**。真正需要调的是**连接池**，判据与真 PG 观察口径见 §9.1。

---

## 8. 工作量与收益

- 机械 `def` 化：约 45 处纯文本替换（app 5 + admin ~40），低风险、自校验强。
- 逐 handler 重构：仅 `import_document`（+ 登录/OIDC 类的 `await` 分布确认）。
- 预计 1 个 commit，自验（§6）后推送。
- **收益**：关闭架构债 #1 中"唯一仍结构性威胁可水平扩展目标"的残留，且 `def` 化可一次性批量消除，性价比最高（架构师原话）。

---

## 9. 实施记录（已落地）

**决策落地（用户拍板，2026-09-22）**：
- **A（风格统一）**：将已用 `run_in_threadpool` 包裹的 6 个 handler 也 `def` 化并去 `await` 包裹 —— 直接同步调用被调函数，由 FastAPI 默认线程池统一 offload（与 Phase 1 的 `def` 路由走同一条路径，不再有 `run_in_threadpool` 双重包裹）。
- **B（import_document）**：保留 `async def`；其内 4 处 `database.*` / 重发布调用已用 `run_in_threadpool` 包裹（1515/1526/1553/1565），`await file.read()/close()` 维持异步文件流，不改动。
- **C（池尺寸）**：本次**不**调 `database_pool_size` / Starlette 线程池；先改代码、观察 `slow_db_ms` 慢查询日志，按需 follow-up。

**实际改动**：
- `app.py`：17 个无 `await` 路由 `async def` → `def`（Phase 1，脚本机械判定"函数体无 `await`"）。
- `admin_app.py`：
  - 57 个无 `await` 路由 `async def` → `def`（Phase 1）。
  - 6 个 `run_in_threadpool` 路由 `def` 化 + 去 `await` 包裹：`publish_version`(682)、`create_evaluation_run`(928)、`model_config`(1342)、`update_model_config`(1383)、`list_artifacts`(1437)、`create_artifact`(1441)。
  - `import_document`(1482) 维持 `async`。
- 残留 `async def` 路由：仅 `import_document`。中间件 `request_logging`/`request_boundary`、异常处理器 `http_error` 保持 `async`（非路由，不转）。

**验证**：
- 静态：`ast` 解析两个文件；遍历 AST 确认所有同步路由 handler 体内无 `await`，且 `run_in_threadpool` 仅出现在 `import_document` 与 import 语句。
- 运行时：`venv` 下 `import app, admin_app` 成功；`pytest tests/test_stress_concurrency.py tests/test_database_migrations.py` 6 passed；全量 `pytest` 回归（目标保持 232 passed / 4 skipped）。
- 实施坑（已规避）：同文件并行 Edit 存在写竞争，部分编辑被基于陈旧文件态的后续写覆盖；改用「单次读-单次写」脚本原子化修复，并以 AST 断言兜底。

**提交**：单 commit `refactor: offload blocking DB calls from event loop by def-ifying route handlers`，经 Git Data API 推送（SHA 保留）。

---

## 9.1 决策 C 实测补充（2026-09-23）

> 目的：把决策 C「要不要调池尺寸」从"凭推断"变成"有实测判据"。**不改变本次已推送的代码**（`d2339ef` 已闭环，CI run `35781968760` success），只补观测口径、一处事实更正，以及随后落地的 CI 回归（见 §六）。

### 一、事实更正：线程上限是 **40**，不是 32

实测环境：python 3.13.3 / anyio **4.15.1** / starlette 1.6.0 / fastapi 0.141.1 / sqlalchemy 2.0.54，本机 `cpu_count=32`。

三条独立证据一致：

| # | 方法 | 结果 |
|---|---|---|
| 1 | 事件循环内内省 `anyio.to_thread.current_default_thread_limiter().total_tokens` | **40**（类型 `CapacityLimiter`）|
| 2 | 裸压 `starlette.concurrency.run_in_threadpool`：64 并发 × 300ms 阻塞 | 峰值并发 **40**，wall 0.65s（= `ceil(64/40)=2` 波）|
| 3 | 真 app 的 `/api/history`（`app.py:392`，DB 型 `def` 路由）压测 | 见下表，峰值并发封顶 **40** |

| offered concurrency | 8 | 16 | 40 | 64 | 128 |
|---|---|---|---|---|---|
| 实测峰值并发 | 8 | 16 | **40** | **40** | **40** |
| p50 (ms) | 60.3 | 161.7 | 414.2 | 591.4 | 883.0 |
| p95 (ms) | 87.9 | 197.4 | 492.6 | 926.8 | 1191.2 |
| 失败数 | 0 | 0 | 0 | 0 | 0 |

**为什么不是 32**：`min(32, cpu_count+4)` 是 `concurrent.futures.ThreadPoolExecutor` 的默认值；anyio 4.x 的 `to_thread` 使用自带的 `WorkerThread` 池，**不经过 `ThreadPoolExecutor`**，其并发闸门是默认 `CapacityLimiter(40)`。本文档 §2 与 §5 早先的"ThreadPoolExecutor 40 线程"说法**数字巧合、对象错误**，已在上文更正。**不要再按 32 推算容量。**

### 二、连接池那一半在本地**结构上测不到**

`backend/database.py:74-79` 的 sqlite 分支只设 `connect_args={"check_same_thread": False}` 与 `poolclass=NullPool`，**`pool_size` / `max_overflow` / `pool_timeout` 根本不传给 `create_engine`**——它们只在 `postgresql` 分支（`backend/database.py:80-86`）生效。运行时核对 `db.engine.pool.__class__.__name__ == "NullPool"` 已确认。

因此 `database_pool_size` / `database_max_overflow` 在 SQLite 下是**死配置**，本机不具备复现池饱和的条件。**池侧结论只能由真 PG 环境给出，任何本地数字都不许当作池的结论。**

### 三、单变量控制实验：只调线程侧**无效**

固定 `offered concurrency=96` / `requests=192`（均不变），**唯一变量** = anyio 令牌数（以显式 `CapacityLimiter(T)` 注入 `run_in_threadpool`）：

| T (tokens) | 峰值并发 | wall | 吞吐 rps | p50 (ms) | 失败 |
|---|---|---|---|---|---|
| 40（默认）| 40 | 1.75s | 109.4 | 733.0 | 0 |
| 64 | 64 | 1.81s | 105.8 | 686.9 | 0 |
| 96 | 96 | 1.93s | 99.5 | 848.6 | 0 |

- 令牌数**确实是生效的旋钮**（峰值并发随之抬到 64 / 96）；
- 但在 SQLite 下**吞吐不升反微降**：增加线程只是增加争用（`NullPool` 每次 checkout 重开连接 + 文件锁序列化），事件循环本身早已不是瓶颈。

**结论：单独调线程池不是有效的优化手段，必须与连接池一起评估。**

### 四、真 PG 环境的观察口径（runbook）

观测钩子已被证明可用：把 `IT_SLOW_DB_MS` 降到 5ms 时真能打出 `event=slow_db_query`（10–16ms 级）；默认阈值下 192 请求只有 2–11 条 `slow_request`——**钩子没问题，是阈值与原 SQLite 场景的问题**。

- **开关**：`IT_SLOW_DB_MS`（默认 200）、`IT_SLOW_REQUEST_MS`（默认 1000）。慢事件是结构化 JSON，按 `event=slow_db_query` / `event=slow_request` 过滤（实现见 `backend/database.py:105-124`、`app.py:189-194`）。
- **灌流量**：`scripts/bench_concurrency.py`（离线并发探针，人工触发，**不是门禁断言**）。
- **看三件事**：
  1. `slow_db_query.duration_ms` 是否随并发**阶梯上升**——若上升，是**连接池排队**，不是单条 SQL 变慢；
  2. `slow_request` 是否**集中在 DB 型路由**（`/api/history`、`/api/admin/documents`、`/api/usage/ledger` 等），而 `/api/query` 之类已 offload 的路由相对干净；
  3. 有无 `TimeoutError`（`pool_timeout=30s`）或 5xx——**出现即池容量硬不足，是"必须调池"的判决性证据**。
- **判据（约束链）**：anyio 令牌 **40** → PG 池 `pool_size(5) + max_overflow(10) = 15` → 40 个 `def` handler 同时进 DB 时最多 15 个拿到连接，其余 25 个排队至 30s。**线程上限 > 池容量 ⇒ 池才是真瓶颈**。
- **若要调**：先调 `IT_DB_POOL_SIZE` / `IT_DB_MAX_OVERFLOW`，并满足 `pool_size + max_overflow ≤ PG max_connections − 预留`（**再乘进程/worker 数**，PG `max_connections` 是实例级共享的）。**不建议**顺手调大 anyio 令牌：在池不变时它只把排队从池内挪到线程侧，收益为零（§三已实测）。
- **不建议的触发条件**：仅在无 PG 的本地 SQLite 上看到 `slow_db_query` 就调池——那基本是 SQLite 连接/锁开销，与 PG 池无关。

### 五、证据留存

探针脚本与原始输出在 **`D:/Temp/dm_probe/`**（`probe_c.py` 压测矩阵、`probe_c2.py` 单变量控制实验、`probe_c.out` / `probe_c2.out`），**不在仓库内**，工作树保持干净。本节所有数字均来自这两个脚本的实跑。

### 六、已落地：池侧观测的自动化回归（2026-09-23，`tests/test_connection_pool.py`）

本地探针只覆盖线程侧；池侧此前只能靠人工观测。现已把「池容量是并发上界」做成 CI 上的断言：

- `PostgresConnectionPoolTests`（需 `IT_TEST_POSTGRES_URL`，CI 的 `pgvector/pgvector:pg17` service 提供）对**可丢弃的 PG 库**（自建自毁 + `alembic upgrade head`）发 **64 并发 × 128 次 `POST /api/query` 写请求**，断言：① 全部 200；② 峰值同时占用连接数 **> `pool_size`**（溢出槽真的被用上）；③ 峰值 **≤ `pool_size + max_overflow`**（池是并发上界）；④ `queries` 行数 == 请求数（并发下无写丢失）。
- 池尺寸刻意设为 `pool_size=3` / `max_overflow=5`，容量 8 **远小于** anyio 令牌 40 —— 这样测的是**池**而不是线程限制器；顺序反了就测不到池。
- 实测数字（池形状、峰值占用、HTTP 与 SQL 延迟分位、超 `IT_SLOW_DB_MS` 阈值的语句数）作为 CI artifact **`pool-concurrency-report`** 上传，**是证据不是门禁**：CI 里不做延迟断言，墙钟阈值只会变成 flaky 门禁。
- 同文件的 `SqlitePoolShapeTests` 在无 PG 时也运行，固定「sqlite 分支不向 `create_engine` 传 `pool_size`/`max_overflow`、得到 `NullPool`」这一事实（§二 的结构性依据），防止它悄悄失效。
- 据此 `PROJECT_STATUS.md` 的「并发能力：未验证」已改写为「**池的有界性**已在 CI 上验证，**容量**仍未验证」——测出的 rps/延迟是单进程 + `TestClient` + 极小数据的数字，不可当容量用。

### 七、慢日志聚合工具（2026-09-27 新增）

把 §四 的「读慢日志、按 event 过滤、看三件事」落成一个**开箱即用的分析器**：`scripts/analyze_slow_logs.py`。喂入 `data/logs/*.err.log`（或 `--since 1h` 看最近一小时），它聚合 `slow_db_query` / `slow_request` / `slow_admin_request` 的事件数 + `duration_ms` 分位，按路径列出 `slow_request` 分布，并直接给出决策建议（慢 SQL 看 `slow_db_query`、池饱和看 `slow_request` + `pool_timeout`/`TimeoutError`/5xx、都不是则默认够用）。

- 用法：`python scripts/analyze_slow_logs.py` / `--since 1h` / 指定文件 / `--slow-db-ms` / `--slow-request-ms`。
- 已在历史日志（`data/logs/*.err.log`，2026-09-23）上实跑验证：解析正常，`--since` 相对/绝对时间过滤均生效；该批日志含 6 条 `slow_admin_request`（均在 `/api/admin/model-config`，p50≈2738ms），但**无 `slow_db_query`、无 5xx、无 pool-timeout** —— 属模型运行时延迟，非 DB 连接池饱和，与 §四 判据一致（池饱和必须伴随 `TimeoutError`/5xx）。
- 这是"观察慢日志再决定"的落地工具；容量结论仍需在真 PG + 真实流量下跑出来再喂给它。

