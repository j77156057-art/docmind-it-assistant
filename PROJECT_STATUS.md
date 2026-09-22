# Project Status

更新日期：2026-09-22

## 已完成

- 查询服务与管理服务拥有独立入口和能力边界。
- PostgreSQL/SQLite Repository 与五个可升降级 Alembic 迁移。
- Markdown、TXT、PDF、DOCX 版本化导入和全文 + 向量混合检索。
- OIDC Bearer Token 校验、`viewer/auditor/admin` RBAC 和匿名化主体隔离。
- 用户、组、角色级文档 ACL，并在召回前执行过滤。
- 文档密级（`public/internal/confidential`）参与授权：与 ACL 构成"与"条件，只收紧不放大；机密需 `document.read.confidential`（等级 ≥ auditor），未知密级失败关闭，导入只能提升密级不能降低。
- OIDC 浏览器登录（Authorization Code + PKCE）：`/api/auth/oidc/start` 发起授权并下发 S256 `code_challenge` + `state`/`nonce` 流程 Cookie（SameSite=Lax），`/api/auth/oidc/callback` 换码、校验 `state`/`nonce` 与 `alg`（拒绝 HS256 算法混淆）后发放会话 Cookie（SameSite=Strict）；端点默认走 issuer 的 `.well-known/openid-configuration` 自动发现，可用 `IT_OIDC_*_ENDPOINT` 覆盖；Bearer Token 通路保留给 API 客户端。
- 审计导出（A-4 第一项）：`/api/admin/audit-events/export` 导出 CSV/JSON，支持 `start`/`end`/`action`/`target_type`/`actor` 筛选与 10000 行上限；导出动作自身写审计（`audit.export`，`target_type=audit_export`），导出文件带 BOM 便于 Excel 打开中文。
- 管理后台文档导入、ACL 编辑、审计、健康状态和运行时模型切换。
- DOCX、PDF、PPTX、XLSX 结构化生成、回读验证和受控下载。
- 模型重试、Token/费用账本、动态模型路由和密钥脱敏。
- 知识治理：版本状态机（`queued/processing/staged/indexed/rejected/withdrawn/superseded/failed`）、能力制治理角色、审批发布与职责分离、作废与回滚、审核预览与 `document_version_reviews` 留痕。
- 异步索引：`ingestion_jobs` 业务队列、`worker` 进程（抢占、心跳、僵尸回收、可重试性分类）、导入接口 `202 + job_id`、任务重试/取消、队列健康诊断；`reindex`/`withdraw` 任务类型已由 Worker 实现（reindex 重置版本为 `queued` 后重嵌、withdraw 走既有治理方法并写审计），可重试失败通过行内 `next_attempt_at` 持久化指数退避（迁移 `20260922_0011`），避免坏上游被紧循环打爆。
- 可选 LangGraph 编排引擎：分批向量化 + checkpoint 断点续跑、独立 checkpoint 存储（不污染应用 schema）、从 `app.py` 出发的 import 闭包边界守卫；PostgreSQL 路径由 CI 上的集成测试覆盖（独立 schema、续跑、`alembic` 无差异）。
- 发布前评测门：`evaluation_cases` / `evaluation_runs` / `evaluation_case_results`，复用生产检索路径的 recall@k、引用命中率、拒答正确率与基线回归，`off/warn/block` 三种门禁模式与 `override_gate` 留痕。
- 部署编排包含索引 Worker：`scripts/dev.ps1` 启停三个服务（查询/管理/Worker），`compose.yaml` 增加 `worker` 服务并在管理服务与管理端共享 `docmind_sources` 卷、独立 `docmind_worker` 卷保存 checkpoint。
- PowerShell 本地启停脚本与 Docker Compose 本地 PostgreSQL 环境。
- 自动测试、依赖漏洞扫描和 Dependabot 更新。

## 安全边界

- 查询服务不注册上传、ACL 写入、办公产物生成或命令执行路由。
- 只有 `indexed` 版本参与召回；`staged`、`rejected`、`withdrawn` 的分块已入库但任何召回路径都取不到。
- 导入不能修改已存在文档的访问范围，范围调整只能通过需要 `acl.write` 且写审计的 ACL 接口。
- 治理角色不进入 ACL 解析，授予审核或发布角色不会扩大文档可见范围。
- `admin` 不自动获得审核与发布能力；越权必须由配置开关加显式请求共同触发，并记录 `is_override`。
- 索引任务表只保存业务元数据（版本、发起人、请求号、错误码），不写入文档正文。
- 查询进程既不注册导入接口也不启动 Worker；Worker 使用独立的 `python -m worker` 入口。
- 查询进程的 import 闭包内不得出现 `langgraph`、`langchain_core` 或 `langsmith`；该规则由自动测试从 `app.py` 递归验证，而非人工评审。
- LangGraph 及其 32 个传递依赖只存在于 `requirements-worker.txt`；查询与管理部署的依赖集合保持不变。
- 编排框架的 checkpoint 不写入应用 schema（独立 SQLite 文件或独立 PostgreSQL schema），避免 `alembic check` 失真。
- 代码不设置 `LANGSMITH_TRACING` / `LANGCHAIN_TRACING`；LangSmith 默认关闭，启用需先做脱敏评审。
- 评测用例与逐题结果只保存问题、计数与排名，不保存模型回答或知识正文。
- 可观测性（A-5）：进程内指标注册表（请求计数/延迟分位、ingestion 队列深度/失败数、模型费用汇总）由 `GET /api/admin/metrics`（`audit.read`）暴露；请求中间件按 `IT_SLOW_REQUEST_MS`、数据库按 `IT_SLOW_DB_MS` 写慢操作 `WARNING` 日志；离线并发压测 `scripts/bench_concurrency.py`。
- 越权放行评测门必须同时满足：配置开关打开、主体同时持有 `document.publish` 与 `governance.override`、请求显式声明，并写入 `override_gate` 审批记录。
- `development` 认证只允许配置为回环地址；生产配置强制 OIDC。
- API Key 只从进程环境或未提交的 `.env` 读取。
- 日志和审计不保存问题正文、回答正文、Token 原文或供应商错误正文。
- 生成文件固定在专用目录，并拒绝路径穿越和电子表格公式注入。
- 日志字段是白名单（标识符、枚举、计数、布尔），问题正文、回答正文与文档内容无法通过 `log_event` 的额外字段进入日志；白名单有专门测试固定。
- 字段级加密（A-4）：`query.question` 按 `IT_QUERY_FIELD_KEY` 用 Fernet（AES-128-CBC + HMAC-SHA256）加密落库，写入前加密、读出按需解密；未带 `enc:v1:` 前缀的旧明文记录仍可正常读出（向后兼容，无需迁移）。密钥缺失时本地/测试用固定开发密钥并告警，生产环境 `AppSettings` 校验强制要求配置 `IT_QUERY_FIELD_KEY`。`EvaluationCaseRecord.question`（黄金评测用例，知识团队所有）明确不加密。

## 验证基线

```powershell
.venv\Scripts\python -B -m unittest discover -s tests -v
.venv\Scripts\python -m alembic upgrade head; .venv\Scripts\python -m alembic check
.venv\Scripts\python -m worker --once
node --check web/admin.js
.venv\Scripts\python -m pip check
```

`alembic` 与 `worker` 使用 `.env` / 进程环境里的 `IT_DATABASE_URL`；本机没有可用的 PostgreSQL 时，
先设置 `IT_DATABASE_URL=sqlite:///data/queries.db`，或直接用 `.\scripts\dev.ps1 start`
（它会覆盖为项目内 SQLite 并自动执行迁移，同时启动查询、管理与 Worker 三个进程）。

当前测试覆盖查询/管理隔离、OIDC、RBAC、主体数据隔离、文档 ACL、文档密级、文档解析、版本去重、混合检索、降级、模型网关、Token/费用账本、配置、迁移和日志隐私；`tests/test_document_classification.py` 覆盖密级只收紧的性质（机密对 `viewer` 不可见、ACL 与 clearance 是「与」条件、知识大纲不泄露机密标题、未知密级失败关闭、导入只能提升密级、`/api/query` 端到端只把引用给 auditor）；`tests/test_knowledge_governance.py` 覆盖治理状态机、职责分离、越权拦截、召回隔离与审批留痕；`tests/test_ingestion_jobs.py` 覆盖任务抢占唯一性、心跳回收、重试分类、确定性失败、202 异步导入与队列诊断；`tests/test_indexing_graph.py` 覆盖 checkpoint 断点续跑、staging 丢失后的重建与 schema 隔离，`tests/test_indexing_graph_postgres.py` 覆盖 PostgreSQL 路径（同一组性质，另加信息 schema 级别的"checkpoint 不落在 `public`"断言；仅在设置了 `IT_TEST_POSTGRES_URL` 时运行，CI 里由 `pgvector/pgvector` 服务提供，因为迁移 0003 依赖 `vector` 扩展）；`tests/test_isolation_boundary.py` 覆盖导入闭包、动态导入、反向导入与 Trace 开关；`tests/test_evaluation_gate.py` 覆盖指标计算、生产检索路径复用、`block` 阻断、越权放行留痕、基线回归、空题集与用例能力校验；`tests/test_oidc_login.py` 用 stub IdP（真实 RSA 密钥 + `httpx.MockTransport`）覆盖 PKCE(S256) 挑战、state/nonce 绑定、HS256 算法混淆拒绝、错误签名与端点发现缓存；`tests/test_audit_export.py` 覆盖 CSV/JSON 导出、时间/动作筛选、viewer 无 `audit.read` 被 403、非法格式 400 与导出动作自审计留痕；`tests/test_ingestion_backoff.py` 覆盖行内 `next_attempt_at` 指数退避与退避上限，`tests/test_metrics.py` 覆盖 `audit.read` 读取指标快照、viewer 被 403 与注册表线程安全，`tests/test_stress_concurrency.py` 覆盖并发请求全部成功且指标精确计数，`tests/test_field_encryption.py` 覆盖 `query.question` 字段级加解密、旧明文回退与生产必配 `IT_QUERY_FIELD_KEY` 校验。

## 后续工作

1. **已收口**：`reindex`/`withdraw` 任务类型与可重试失败持久化退避（`next_attempt_at`，迁移 `20260922_0011`）；进程内可观测性（`/api/admin/metrics` + 慢请求/慢查询日志）与离线并发压测（`scripts/bench_concurrency.py`）；`query.question` 字段级加密（Fernet，密钥 `IT_QUERY_FIELD_KEY`，`tests/test_field_encryption.py` 覆盖加解密、旧明文回退与生产必配校验）。
2. 文档保留期 + 保留策略（自动清理任务，默认 365 天，待实现）。
3. `query` 接口限流/配额（每用户每日 1000 次，超阈返回 429 + Retry-After；待实现）。
5. 引用持久化、用户反馈、知识缺口统计（采购硬缺口，零命中，待建表）。
6. 用户/用户组实体表与部门（域 B 组织模型，ACL 的 group/role 当前只是 OIDC claim 字符串）。
7. 检索质量数据集运营（黄金题评审流程）、检索重排与知识域模型（域 B）。
8. 提供 Kubernetes、网络策略、备份恢复和灰度发布参考部署。
9. `evaluate` 异步评测任务类型（评测门已存在，Worker 侧仍 `job_type_unsupported`）。
