# 实现方案预研：异步路由 Handler `def` 化（架构债 #1）

> 状态：已实施（待评审 / 待推送 main）
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

FastAPI 对 `def`（同步）路由 handler 会**自动提交到默认 `ThreadPoolExecutor`** 执行，事件循环得以继续处理其他请求。这正是对"同步 DB 驱动"的官方推荐做法。

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
| 线程池饱和 | 中：默认 `ThreadPoolExecutor` 40 线程；`database` 连接池默认 `pool_size=5`/`max_overflow=10` → 若 40 线程同时查 DB，连接池会排队（`pool_timeout`） | 相比"事件循环被完全卡死"，这是有界、可观测的（已有 `slow_db_ms` 日志）。高 QPS 读面可显式调大 `database_pool_size` 或 Starlette 线程池 `max_workers`（follow-up，不阻塞本次） |
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
