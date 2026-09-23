# DocMind IT Assistant

[![Tests](https://github.com/j77156057-art/docmind-it-assistant/actions/workflows/tests.yml/badge.svg)](https://github.com/j77156057-art/docmind-it-assistant/actions/workflows/tests.yml)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-2E7D32.svg)](LICENSE)

面向企业 IT 知识服务的安全型 RAG 应用。项目将只读查询服务与管理服务拆分，覆盖文档导入、混合检索、OIDC SSO、RBAC、检索前文档 ACL、审计、模型路由和办公文档生成。

> 这是可本地运行的工程化演示，不宣称已经完成生产部署。生产环境仍需要真实身份提供商、托管 PostgreSQL、对象存储、异步任务和基础设施安全策略。

## 界面预览

| 查询工作台 | 管理控制台 |
|---|---|
| ![查询工作台](docs/assets/query-console.png) | ![管理控制台](docs/assets/admin-console.png) |

## 核心能力

- **服务隔离**：查询进程不注册上传、ACL 写入或办公产物生成接口。
- **企业身份**：OIDC Bearer Token 校验签名、`iss`、`aud`、`exp`、`iat` 与算法白名单。
- **最小权限**：`viewer < auditor < admin` 角色继承，查询历史与费用按匿名化主体隔离。
- **文档 ACL**：用户、组和角色条件直接进入召回查询，未授权内容不会先召回再过滤。
- **混合检索**：全文 BM25 与向量余弦召回通过 RRF 融合，Embedding 故障时降级到全文检索。
- **动态模型路由**：知识库、本地模型和云模型可在管理后台切换，无需重启查询服务。
- **可追踪成本**：记录模型每次尝试的 Token、延迟、供应商请求 ID、单价快照和费用。
- **结构化产物**：管理端生成并验证 DOCX、PDF、PPTX、XLSX，不执行任意脚本。
- **知识治理**：导入不再等于发布，版本有状态机、审批职责分离、作废与回滚，全程留痕。
- **异步索引**：导入可排队（返回 `202` + 任务号），Worker 抢占执行、心跳保活、失败按可重试性分类重试。
- **断点续跑编排**：可选的 LangGraph 引擎按批次 checkpoint，Worker 中途被杀不会重复为已完成的向量化付费。
- **发布前评测门**：黄金题集复用生产检索路径量化 recall@k、引用命中率与拒答正确率，`block` 模式可阻断未达标发布，越权放行必须留痕。
- **隐私日志**：日志不记录问题正文、回答正文、查询参数、会话 ID、客户端地址或密钥。
- **引用与反馈闭环**：每次回答的引用来源自动落 `query_citations`（区分 chunk / knowledge 两种形态并 best-effort 解析 `document_chunk_id`）；用户可对回答评分（positive/negative），写入 `query_feedback` 并按 `(query_id, actor)` 幂等；证据不足时登记 `knowledge_gaps` 知识缺口，供运营闭环。
- **组织模型懒同步**：OIDC 登录成功后按主体声明幂等落 `users` / `groups` / `user_group_memberships`，管理后台只读展现；`users.subject_id` 用 HMAC 哈希（与 `document_acl` 用户型同构），不存原始 OIDC `sub`。
- **缺口不复制明文**：`knowledge_gaps` 只保留 `query_id` 弱引用（源 query 删除则 `SET NULL`）与非 PII 摘要（如 `自动登记：insufficient_evidence，路由 cloud`），绝不解密或复制 `query.question` 明文。

## 架构

```mermaid
flowchart LR
    U[查询用户] --> Q[查询服务 :8020]
    A[管理员 / 审计员] --> M[管理服务 :8021]
    Q --> I[OIDC / RBAC]
    M --> I
    Q --> R[全文 + 向量检索]
    R --> D[(PostgreSQL + pgvector)]
    Q --> G[模型路由与费用账本]
    M --> D
    M --> F[文档导入与办公产物]
    M --> G
```

查询和管理服务共享身份规则与数据模型，但拥有不同的 HTTP 暴露面。生产部署应进一步使用独立入口、数据库角色、网络策略和管理端写入挂载。

## 快速启动

环境要求：Windows PowerShell、Python 3.11 或更高版本。推荐 Python 3.13。

```powershell
git clone https://github.com/j77156057-art/docmind-it-assistant.git
cd docmind-it-assistant
.\scripts\dev.ps1 start -Open
```

脚本会创建项目虚拟环境、安装依赖、复制示例配置、使用项目内 SQLite、执行迁移，并在后台启动：

- 查询工作台：`http://127.0.0.1:8020/`
- 管理控制台：`http://127.0.0.1:8021/`
- 索引 Worker：无端口，随脚本一起启停（`status` 会显示 `running/stopped`）

脚本会强制 `IT_INGESTION_WORKER_ENABLED=true`（进程环境优先于 `.env`），因为 Worker 已经启动：导入会返回 `202` 并入队，由 Worker 完成解析与向量化。管理端"导入任务"视图可以看到队列与重试。

```powershell
.\scripts\dev.ps1 status
.\scripts\dev.ps1 stop
```

默认 `development` 认证会授予本机演示用户全部角色，并且配置层强制查询和管理地址均为回环地址。不要把开发认证用于共享网络或生产环境。

需要真实登录页的本机或面试演示环境可启用 `local` 模式：

```powershell
.venv\Scripts\python -c "from backend.auth import hash_password; print(hash_password('请替换为强密码'))"
```

将输出写入 `.env` 的 `IT_LOCAL_PASSWORD_HASH`，并设置 `IT_AUTH_MODE=local`、`IT_LOCAL_USERNAME=admin` 后重启。查询端和管理端都会在未登录时跳转到 `/login`，成功登录后使用 HttpOnly、SameSite=Strict 的限时会话 Cookie。公开部署仍应使用企业 OIDC，由身份平台完成 MFA、离职禁用和用户生命周期管理。

本地登录模式可设置 `IT_GUEST_LOGIN_ENABLED=true` 开启游客入口。每次游客登录都会生成独立的短期身份，只有 `viewer` 接口权限；ACL 检索会主动移除游客的角色和用户组上下文，因此只能查询 `public` 文档，不能访问受限文档、管理后台或其他游客的查询历史。`IT_GUEST_SESSION_HOURS` 控制游客会话有效期，默认 2 小时。

## Docker 本地环境

Docker Compose 会启动 PostgreSQL/pgvector、迁移任务、查询服务、管理服务和索引 Worker，宿主机端口仍只绑定 `127.0.0.1`：

```powershell
docker compose up -d --build
docker compose ps
docker compose down
```

Worker 与管理服务共享 `docmind_sources` 卷（否则 Worker 读不到上传的原文件，会以 `source_missing` 失败），
并把 checkpoint 放在独立的 `docmind_worker` 卷上，容器重建后仍可断点续跑。管理服务在该编排里固定
`IT_INGESTION_WORKER_ENABLED=true`：**启用异步导入就必须同时运行 Worker**，否则任务只会排队，
`/health/ready` 的 `ingestion.stalled` 会一直为真。Worker 刻意不配 healthcheck——它没有 HTTP 端点，
而"队列不消费"是诊断信息，重启容器解决不了。

该 Compose 文件用于本地演示，使用开发认证和示例数据库密码，不是生产部署清单。

## SSO、RBAC 与 ACL

生产配置必须满足：

```dotenv
IT_ENVIRONMENT=production
IT_AUTH_MODE=oidc
IT_DATABASE_URL=postgresql+psycopg://...
IT_EMBEDDING_MODE=provider
IT_OIDC_ISSUER=https://id.example.com/
IT_OIDC_AUDIENCE=docmind
IT_OIDC_JWKS_URL=https://id.example.com/.well-known/jwks.json
IT_OIDC_CLIENT_ID=docmind-portal
IT_OIDC_REDIRECT_URI=https://docmind.example.com/api/auth/oidc/callback
IT_OIDC_SCOPES=openid profile email
IT_AUTH_SUBJECT_SALT=<至少 32 字符的随机值>
# 会话 JWT（HS256）签名密钥。留空则回退到 IT_AUTH_SUBJECT_SALT 派生；生产环境建议独立配置、至少 32 字符。
IT_AUTH_SESSION_SECRET=
# 运行时供应商凭证（API Key）Fernet 加密密钥。留空则回退到 IT_AUTH_SUBJECT_SALT 派生；生产环境建议独立配置。
IT_PROVIDER_CREDENTIAL_KEY=
# query.question 与 document_chunks.content（正文）字段级加密密钥（Fernet）。生产必须配置；可为原始 32 字节 Fernet 密钥或任意口令（SHA-256 派生）。
IT_QUERY_FIELD_KEY=<至少 16 字符的随机值或 Fernet 密钥>
```

生产模式会拒绝 SQLite、开发认证、`trusted_headers` 和本地 Hash Embedding。原始 OIDC `sub` 不入库，而是通过带密钥 HMAC 生成稳定主体标识。

管理端主要接口：

| 方法 | 路径 | 最低角色 | 用途 |
|---|---|---|---|
| `GET` | `/api/admin/documents` | auditor | 查看文档版本、访问范围与原文件状态 |
| `GET` | `/api/admin/documents/{id}/versions/{version}/source` | auditor | 在线查看或下载原文件 |
| `GET/PUT` | `/api/admin/documents/{id}/acl` | auditor/admin | 查看或替换文档 ACL |
| `POST` | `/api/admin/documents/import` | admin | 导入 Markdown、TXT、PDF、DOCX |
| `GET/POST` | `/api/admin/artifacts` | auditor/admin | 查看或生成办公产物 |
| `GET/PUT` | `/api/admin/model-config` | auditor/admin | 查看或切换运行时模型与回答策略 |
| `GET` | `/api/admin/audit-events` | auditor | 查看管理审计记录 |
| `GET` | `/api/admin/audit-events/export` | auditor | 导出审计事件为 CSV/JSON（支持 `start`/`end`/`action`/`target_type`/`actor` 筛选，导出动作本身写审计） |
| `GET` | `/api/admin/metrics` | auditor | 进程内可观测性快照：请求计数/延迟分位、 ingestion 队列深度、失败数与模型费用汇总 |
| `GET` | `/api/admin/governance/pending` | `document.review` | 查看待审核版本 |
| `GET` | `/api/admin/documents/{id}/versions/{v}/preview` | `document.review` | 审核预览该版本的内容块（每次读取写审计） |
| `POST` | `/api/admin/documents/{id}/versions/{v}/review` | `document.review` | 通过或驳回（驳回必须填写意见） |
| `POST` | `/api/admin/documents/{id}/versions/{v}/publish` | `document.publish` | 发布已通过审核的版本 |
| `POST` | `/api/admin/documents/{id}/versions/{v}/withdraw` | `document.withdraw` | 作废当前已发布版本（必须填写原因） |
| `POST` | `/api/admin/documents/{id}/versions/{v}/rollback` | `document.rollback` | 回滚到历史版本（必须填写原因） |
| `GET` | `/api/admin/documents/{id}/versions/{v}/reviews` | `document.read` | 查看该版本的审批记录 |
| `GET` | `/api/admin/ingestion/jobs` | `document.read` | 查看索引任务与队列统计 |
| `POST` | `/api/admin/ingestion/jobs/{id}/retry` | `document.write` | 重新排队失败或已取消的任务 |
| `POST` | `/api/admin/ingestion/jobs/{id}/cancel` | `document.write` | 取消尚未开始的任务 |
| `GET` | `/api/admin/evaluation/cases` | `document.read` | 查看黄金题与门禁阈值 |
| `PUT` | `/api/admin/evaluation/cases` | `evaluation.run` | 新增或更新黄金题（按 `case_key` 覆盖） |
| `DELETE` | `/api/admin/evaluation/cases/{id}` | `evaluation.run` | 删除从未被评测引用的用例（有历史则 409） |
| `POST` | `/api/admin/evaluation/runs` | `evaluation.run` | 立即运行评测（`manual` / `pre_publish`）；带 `"queue": true` 则入队为 `evaluate` 任务并返回 202 |
| `GET` | `/api/admin/evaluation/runs` | `document.read` | 查看评测运行与门禁结论 |
| `GET` | `/api/admin/evaluation/runs/{id}` | `document.read` | 查看逐题结果 |
| `GET` | `/api/admin/citations` | auditor | 查看回答引用来源（chunk / knowledge 两种形态） |
| `GET` | `/api/admin/citations/export` | auditor | 导出引用为 CSV/JSON（utf-8-sig，导出动作本身写审计） |
| `GET` | `/api/admin/feedback` | auditor | 查看用户反馈 |
| `GET` | `/api/admin/feedback/export` | auditor | 导出反馈为 CSV/JSON |
| `GET` | `/api/admin/knowledge-gaps` | auditor | 查看知识缺口（弱引用 `query_id` + 非 PII 摘要） |
| `GET` | `/api/admin/knowledge-gaps/export` | auditor | 导出知识缺口为 CSV/JSON |
| `POST` | `/api/admin/knowledge-gaps/{id}/resolve` | `document.write` | 标记缺口已闭环（body `{resolved_version_id}`） |
| `POST` | `/api/admin/knowledge-gaps/{id}/dismiss` | `document.write` | 标记缺口已忽略 |
| `GET` | `/api/admin/org/users` | auditor | 查看组织用户（HMAC 哈希主体） |
| `GET` | `/api/admin/org/groups` | auditor | 查看组织组与成员计数 |
| `GET` | `/api/admin/org/departments` | auditor | 查看部门元数据 |
| `GET` | `/api/admin/org/users/export` | auditor | 导出组织用户为 CSV/JSON |

### 文档密级（classification）

导入时可指定密级，三档：`public` / `internal` / `confidential`（默认 `internal`）。

密级参与授权，且**只收紧、不放大**：

| 密级 | 还需具备什么才能被检索到 |
|---|---|
| `public` / `internal` | 无附加条件（仍须通过 ACL 判定） |
| `confidential` | 主体额外持有 `document.read.confidential`（等级 ≥ `auditor`） |

最终可见 = **ACL 判定通过** 且 **密级允许**，两者是「与」关系，因此：

- 一份 `access_scope=public` 但密级为 `confidential` 的文档，`viewer` 依然检索不到；
- 只有 clearance 而 ACL 未授权同样检索不到——密级不是万能钥匙；
- 已被显式写进文档 ACL 的 `viewer` 也仍读不到机密文档——密级是附加条件，不是可替代条件。

知识治理角色（编辑/审核/发布）等级为 0、不持 `query.read`，本来就不走检索接口，而是通过能力校验的管理端读写文档，因此不受此规则影响。

无法识别的密级值**失败关闭**（按最严处理），导入入口另外直接拒绝未知值并返回 400。密级在导入时**只能提升、不能降低**：已存在的文档不能靠再次导入把 `confidential` 降为 `internal`，否则那是一条绕过密级的提权路径（与 `access_scope` 的同类守卫一致）。降密目前没有 API 入口，需要 DBA 直接改库。

## 知识治理：从"导入即发布"到审批发布

角色分两层：等级角色 `viewer < auditor < admin` 决定后台读取与配置权限；治理角色按**能力**授权，与等级正交，因此可以强制职责分离。

| 角色 | 能力 |
|---|---|
| `knowledge_editor` | `document.write`（上传、发起变更） |
| `knowledge_reviewer` | `document.review`、`evaluation.run` |
| `knowledge_publisher` | `document.publish`、`document.withdraw`、`document.rollback` |

`admin` 拥有导入与 ACL/模型配置能力，但**不自动拥有审核与发布能力**：单人环境需显式设置 `IT_GOVERNANCE_ALLOW_ADMIN_OVERRIDE=true`，并在操作时勾选越权开关，动作会记录 `is_override` 标记。

版本状态机：

```text
queued → processing → staged → indexed → superseded
                                   ↘ withdrawn
              staged → rejected
              superseded / withdrawn → indexed（回滚，不重新向量化）
```

只有 `indexed` 参与召回。`staged`、`rejected`、`withdrawn` 的分块虽然已入库，但混合检索、全文检索和知识大纲**都取不到**——这是本模块最重要的一条安全不变量，由 `tests/test_knowledge_governance.py` 直接断言。

```dotenv
IT_GOVERNANCE_MODE=review
IT_GOVERNANCE_REQUIRE_SEPARATION_OF_DUTIES=true
IT_GOVERNANCE_ALLOW_ADMIN_OVERRIDE=false
IT_LOCAL_ROLES=admin,auditor,viewer
```

`IT_GOVERNANCE_MODE` 默认 `direct`（保持历史行为：索引完成即发布）；生产建议 `review`。治理角色与本地登录可授予的角色都受白名单约束：写进 OIDC claim 或 `IT_LOCAL_ROLES` 但不在白名单内的角色会被丢弃，不会生效。

导入接口不能修改已存在文档的 `access_scope`：越权修改会被拒绝，访问范围只能通过 ACL 接口调整（需要 `acl.write` 并写审计）。审批动作全部记录在 `document_version_reviews`（只插入、不更新），与安全审计 `audit_events` 各司其职。

治理角色都带 `document.read`：审核者必须能看到被审版本的内容，因此治理角色的**文档库读取范围等同于审计员**（含受限文档），但不会因此获得 ACL 写入、模型配置或导入权限。若企业要求"按知识域隔离审核范围"（例如只允许审核本部门文档），需要先引入知识域模型，那是后续域 B 的工作。

## 异步索引 Worker

默认是同步导入（`IT_INGESTION_WORKER_ENABLED=false`），行为与历史版本一致。开启后，导入端点在校验并落盘后立即返回 `202` 与 `job_id`，解析、分块和向量化交给 Worker：

```dotenv
IT_INGESTION_WORKER_ENABLED=true
IT_INGESTION_WORKER_ID=
IT_INGESTION_POLL_SECONDS=2
IT_INGESTION_MAX_ATTEMPTS=3
IT_INGESTION_JOB_TIMEOUT_SECONDS=600
IT_INGESTION_HEARTBEAT_SECONDS=30
IT_INGESTION_BACKOFF_MAX_SECONDS=1800
IT_SLOW_REQUEST_MS=1000
IT_SLOW_DB_MS=200
# query.question 与 document_chunks.content（正文）字段级加密密钥（Fernet）。生产必须配置；未配置则使用固定开发密钥并告警。
IT_QUERY_FIELD_KEY=
# 每用户每日查询配额（0 关闭）。超限返回 HTTP 429 + Retry-After 与 X-RateLimit-* 头。
IT_QUERY_DAILY_QUOTA=1000
# 文档保留期（天）：超过该时长的文档先软标记 expired_at，再经宽限期后硬删（先软后硬）。
IT_RETENTION_DAYS=365
# 保留期到点后的宽限期（天）：软标记再经过该时长才物理删除，期间记录可恢复。
IT_RETENTION_GRACE_DAYS=30
# 保留期清理由 cron 调用 `python scripts/purge_expired.py`（stage=both，幂等）。
```

```powershell
.venv\Scripts\python -m worker
.venv\Scripts\python -m worker --once --max-jobs 50
```

- **任务表是业务队列，不是框架内部状态**：`ingestion_jobs` 一个 `(job_type, version_id)` 一行，管理端能看、能重试、能取消，审计不依赖任何编排框架的 checkpoint 格式。
- **抢占**：PostgreSQL 用 `FOR UPDATE SKIP LOCKED`，多副本 Worker 不会重复处理；SQLite 没有该子句，用条件更新兜底（开发/测试单实例）。
- **心跳与回收**：Worker 在每个步骤后写心跳；超过 `IT_INGESTION_JOB_TIMEOUT_SECONDS` 未心跳的任务被回收重排，尝试次数用尽则判为失败。`IT_INGESTION_HEARTBEAT_SECONDS` 必须小于任务超时，配置层会拒绝非法组合。
- **失败分类**：`embedding_timeout`、`embedding_unavailable`、HTTP 429/5xx 属可重试，自动重排；解析失败、配置错误、文件缺失属确定性失败，立即终止并写入 `last_error_code`。重试次数用尽后由管理员在任务视图人工决定是否重试。
- **隐私**：任务表只存业务元数据（版本、发起人、请求号），不存文档正文。
- **可观测**：`/health/ready` 返回 `ingestion` 诊断块（`stalled`、`queued`、`running`、`failed`）。它**刻意不放进 `checks`**：队列停滞意味着"没有 Worker 在消费"，重启 Pod 解决不了，因此不应让就绪探针失败。进程内指标（请求计数/延迟分位、ingestion 队列深度/失败数、模型费用汇总）由 `GET /api/admin/metrics` 暴露，多 Worker 部署时由各实例轮询后外部抓取；慢请求（>`IT_SLOW_REQUEST_MS`）与慢查询（>`IT_SLOW_DB_MS`）会写 `WARNING` 日志便于排查。并发压测用 `scripts/bench_concurrency.py`（离线、无网络）。
- **Worker 与治理模式共用配置**：`direct` 模式下索引完成即发布，`review` 模式下停在 `staged` 等审批。

### 断点续跑引擎（可选）

默认引擎 `simple` 是纯 Python 顺序执行，不需要框架依赖。切换到 `langgraph` 后，解析→分批向量化→落库→定稿被编排成带 checkpoint 的图，**每批向量化后都会落检查点**，因此 Worker 在批次中途被杀时，恢复运行只处理剩余批次：

```powershell
python -m pip install -r requirements.txt -r requirements-worker.txt
.venv\Scripts\python -m worker --engine langgraph
```

```dotenv
IT_INGESTION_ENGINE=langgraph
IT_INGESTION_CHECKPOINT_PATH=data/worker-checkpoints.db
```

工程约束（由 `docs/isolation-boundary.md` §2.1 与自动测试共同保证）：

- **只进 Worker**：查询进程的 import 闭包内不得出现 `langgraph`/`langchain_core`/`langsmith`，由 `tests/test_isolation_boundary.py` 从 `app.py` 出发递归验证（我实测过：在 `backend/` 里插一行 `import langgraph` 会让测试立刻变红并打印命中路径）。
- **依赖可选**：LangGraph 及其 32 个传递依赖在 `requirements-worker.txt` / `requirements-worker-lock.txt`；查询与管理部署仍只装 39 个包的 `requirements.txt`，核心依赖集合**零变化**。
- **业务状态仍归本项目**：任务可见性、重试与审计读 `ingestion_jobs` 与 `document_versions`；框架 checkpoint 只存续跑所需的内部状态，任何服务都不读它。
- **checkpoint 不写应用 schema**：默认独立 SQLite 文件；PostgreSQL 下写入独立 schema。否则 `alembic check` 会报出未知表、迁移历史失真——`tests/test_indexing_graph.py` 里有一条测试专门跑完图之后验证 `alembic check` 仍然干净。
- **LangSmith 默认关闭**：代码不设置任何 `LANGSMITH_TRACING` / `LANGCHAIN_TRACING`（有测试禁止），启用只能是运维的显式动作，且需先完成脱敏评审。

容器镜像：仓库自带的 `Dockerfile` 只安装 `requirements.txt`，因此镜像里可以直接跑
`python -m worker --engine simple`；要用 `langgraph` 引擎需要在该镜像上补装 Worker 附加依赖：

```dockerfile
COPY requirements-worker.txt ./
RUN python -m pip install --no-cache-dir -r requirements-worker.txt
```

## 评测门与黄金题

"发布"不应只靠人工直觉。黄金题集让每次发布前都有可量化结论：

```dotenv
IT_EVAL_GATE_MODE=warn          # off | warn | block
IT_EVAL_ALLOW_OVERRIDE=false
IT_EVAL_MIN_RECALL=0.8
IT_EVAL_MIN_CITATION_ACCURACY=0.5
IT_EVAL_MAX_REGRESSION=0.05
IT_EVAL_TOP_K=5
```

**最重要的一条设计约束：评测复用生产检索路径。** 执行器调用的是 `HybridRetriever.retrieve`
（同一套嵌入、ACL 组装、混合检索与降级），而不是另写一份 SQL。否则指标描述的是"没人用的系统"，
这是评测体系最常见的失败方式——`tests/test_evaluation_gate.py` 里有一条测试直接统计
`hybrid_search` 的调用次数来钉住这一点。

指标定义（都是"应答题"为分母，因此 `citation_accuracy ≤ recall_at_k` 恒成立）：

| 指标 | 定义 |
|---|---|
| `recall@k` | 命中期望文档的题数 ÷ 需要命中的题数 |
| `citation_accuracy` | 命中且章节定位正确的题数 ÷ 需要命中的题数 |
| `refusal_accuracy` | 正确拒答的题数 ÷ 应拒答题数 |

门禁行为：

- `off`：不执行发布前评测，`gate_result` 记为 `NULL`——"没评测"与"评测通过"永不混淆。
- `warn`：照常发布，但记录结论与原因。
- `block`：指标不达标（或相对基线回退超过 `IT_EVAL_MAX_REGRESSION`）时**阻断发布**，返回 `409`。
- 黄金题为空时只告警不阻断：否则第一天谁都无法发布；但它在界面上是明确的"未判定"，不是静默通过。

**越权放行需要三件事同时成立**：请求显式带 `override: true`、`IT_EVAL_ALLOW_OVERRIDE=true`、
且主体同时持有 `document.publish` 与 `governance.override`。只持有发布权不能绕过质量门禁——
这正是把"能发布"和"能无视度量"分开的原因。放行会写入一条 `override_gate` 审批记录（`is_override=true`）。

评测的身份是固定的（`system:evaluation` + `viewer` 角色），因此结论不取决于谁点了按钮。
代价是：**黄金题只能引用 viewer 可见的文档**；受限文档若要在评测中命中，需要给它加上
`role:viewer`（或等待后续的"知识域"模型，见域 B）。

## 采购硬缺口信号（引用 / 反馈 / 知识缺口 / 组织模型）

回答质量与组织信号不应要求用户额外操作或改动召回热路径。本轮在**不切换 ACL 写入/执行路径、
不改写召回 SQL** 的前提下，新增 8 张表，并在查询落库与登录环节做**非致命**接线（异常只记
`WARNING` 日志，绝不阻断用户答案或登录）：

- `query_citations`：一次回答的引用来源，区分 `chunk`（指向 `document_chunks`）与 `knowledge`
  （指向 `knowledge.md`）两种形态，best-effort 解析 `document_chunk_id` / `document_version_id`；
  对 `queries.id` 级联删除。
- `query_feedback`：用户对回答的评分（positive/negative），按 `(query_id, actor_subject_id)`
  幂等 upsert。
- `knowledge_gaps`：证据不足（`result.evidence == "insufficient"`）时自动登记的知识缺口；
  **只存 `query_id` 弱引用（`SET NULL`，源 query 删除不丢缺口）与非 PII 的 `gap_summary`**
  （如 `自动登记：insufficient_evidence，路由 cloud`），**绝不解密或复制 `query.question` 明文**；
  状态机 `open → addressed`（resolve，需 `resolved_version_id`）/ `dismissed`（dismiss）。
- `users` / `groups` / `user_group_memberships` / `departments` / `user_department_record`：
  最小范围的组织模型。`users.subject_id` 用 HMAC 哈希（与 `document_acl` 用户型同构，零迁移）；
  `departments` 纯元数据、不进 ACL；不存原始 OIDC `sub`。

接线语义：

- **引用 / 缺口落库**：`/api/query` 在拿到 `result` 后非致命地调用 `record_citations` 与
  `register_knowledge_gap`；`POST /api/feedback`（需登录 `viewer`，校验 `rating ∈ {positive,negative}`
  且 `comment ≤ 1000`）调用 `record_feedback`。
- **登录懒同步**：`/api/auth/oidc/callback` 登录成功后非致命地调用 `sync_org_on_login(principal)`，
  按主体声明幂等 upsert `users` / `groups` / `user_group_memberships`；失败仅告警，不影响登录。
- **管理只读展现**：上述信号的列表与导出（CSV 用 `utf-8-sig` BOM、列名 `lower_snake_case`、
  首列 `id` 末含 `created_at`，与 `audit-events/export` 一致）均经 `audit.read` 鉴权并自审计；
  缺口 `resolve` / `dismiss` 需 `document.write`。导出动作的 `target_type` 为 `<entity>_export`，
  缺口闭环/忽略的审计动作为 `knowledge_gap.resolve` / `knowledge_gap.dismiss`。

完整设计见 [采购硬缺口架构](docs/architecture-procurement-gaps.md)，表结构与端点签名以该文档为准。

## 模型与密钥

回答策略可在管理后台选择：`knowledge_first`（默认，命中后直接返回知识库）、`generative_first`（先按 ACL 检索，再交给模型组织客服回答）或 `hybrid`（简单问题直接返回，复杂问题生成式回答）。生成式提示词只包含当前用户可见的检索片段，并保留来源引用；模型不可用时会降级为确定性知识答案或安全拒答。知识证据不足时，可路由到 Ollama、llama.cpp 或 OpenAI 兼容云供应商。云端密钥可以来自进程环境、未提交的 `.env`，或管理后台加密保存的运行时凭据；接口响应、页面与审计日志不会回显明文。

云端模式可直接在“系统状态 → 模型配置”填写供应商、模型和 API Key。系统会先发起一次真实的短请求验证连接，再把密钥用 `IT_AUTH_SUBJECT_SALT` 派生的密钥加密保存；页面和接口只返回“已配置”，不会回显明文。切勿随意更换 `IT_AUTH_SUBJECT_SALT`，否则已保存密钥将无法解密；生产环境更推荐将密钥放入部署平台的 Secret Manager 或环境变量。

用户提问（`query.question`）与文档正文（`document_chunks.content`）按 `IT_QUERY_FIELD_KEY` 做字段级 AES 加密落库，读取时解密，密钥缺失时本地/测试使用固定开发密钥并告警。加密值带 `enc:v1:` 前缀，未带前缀的旧明文记录仍可正常读出（向后兼容，无需迁移）。文档正文 `search_text`（词法检索）与 `embedding`（语义向量）保持明文/原样；存量明文正文由迁移 `20260922_0014` 就地回填加密（幂等、可重复运行），旧明文记录读取时原样返回。`IT_QUERY_FIELD_KEY` 与 `IT_AUTH_SUBJECT_SALT` 一样属于密钥，生产环境必须配置且建议存入 Secret Manager；轮换密钥时需对存量记录做一次重加密。

每个用户每日查询次数受 `IT_QUERY_DAILY_QUOTA`（默认 1000）限制，按 UTC 自然日重置；超限返回 `HTTP 429` 并带 `Retry-After`（距次日 UTC 零点秒数）与 `X-RateLimit-Limit`/`X-RateLimit-Remaining` 头。`development` 认证模式与匿名/空主体跳过限流（本地离线演示不受影响）。计数进程内维护、重启清零，适用于单实例部署。

### 文档保留期（先软后硬）

文档超过 `IT_RETENTION_DAYS`（默认 365 天）后进入保留期末尾：保留期清理任务先在 `documents` 表打上 `expired_at` 软标记（记录仍可恢复，满足合规留痕），再经过 `IT_RETENTION_GRACE_DAYS`（默认 30 天）宽限期后物理删除该文档及其全部子表（`document_versions`/`document_chunks`/`document_acl`/`ingestion_jobs`/`document_version_reviews`/`model_usage_ledger`/`evaluation_runs` 等），从而回收存储。保留范围仅限文档主表及其级联子表，不影响查询记录与审计事件。

清理由管理端触发或 cron 调用：`GET /api/admin/retention/preview` 预览将要软标记/硬删的数量（需 `audit.read`，自身计入审计），`POST /api/admin/retention/purge`（`{"stage": "soft"|"hard"|"both"}`，默认 `both`，需 `document.write`）执行；`scripts/purge_expired.py` 是 cron 入口，幂等可重复运行。

管理后台切换到本地模型时，会先执行一次真实的短请求：Ollama 会调用 `/api/generate` 将目标模型加载并确认它出现在 `/api/ps`；llama.cpp 会调用兼容的 `/chat/completions`。探活失败不会保存新配置，系统状态页会显示“服务不可达 / 模型未安装 / 尚未启动 / 已启动”。Ollama 可用 `ollama serve` 启动服务，llama.cpp 需先运行自己的 `llama-server`（默认 `127.0.0.1:8080`）。

注意：Ollama 和 llama.cpp 是两种不同的本地供应商。即使模型文件名称相近，Ollama 中的模型（例如 `qwen3.6:35b-a3b`）也不会让 `llama.cpp / qwen3.6-35b-a3b` 自动变为可用；必须在后台把供应商切换为“本地 Ollama”。

管理后台导入的原文件会按 `data/sources/<document-id>/v<version>-<filename>` 保存，文档详情可以直接查看或下载。历史上只保留索引、未保存原件的版本会明确显示“该历史版本未保留原文件”。

```dotenv
IT_MODEL_MODE=knowledge
IT_LOCAL_PROVIDER=ollama
IT_LOCAL_MODEL=qwen2.5:7b
IT_CLOUD_PROVIDER=qwen
IT_CLOUD_MODEL=qwen-plus
```

供应商配置和全部环境变量见 [.env.example](.env.example)。

## 数据库与迁移

```powershell
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m alembic current
```

迁移可升级、可降级；涉及数据删除的降级必须先备份。细节见 [migrations/README.md](migrations/README.md)。

### PostgreSQL 必须安装 pgvector

迁移 `20260920_0003_documents_hybrid_search` 在 PostgreSQL 上执行 `CREATE EXTENSION IF NOT EXISTS vector`（SQLite 路径不执行），向量列与 HNSW 余弦索引都依赖它。数据库服务端**没有** pgvector 时，`alembic upgrade head` 会在这一步直接失败：

```
sqlalchemy.exc.NotSupportedError: (psycopg.errors.FeatureNotSupported) extension "vector" is not available
DETAIL:  Could not open extension control file "/usr/share/postgresql/16/extension/vector.control": No such file or directory
```

两条安装路径，选一条即可：

| 部署方式 | 做法 |
|---|---|
| 容器 | 用官方镜像 `pgvector/pgvector:pg16` 或 `:pg17` 代替 `postgres`（本仓库 `compose.yaml` 用的是 `pgvector/pgvector:pg17`）。**不要**把镜像换回裸 `postgres`。 |
| 自建实例 | 先装发行版扩展包：Debian/Ubuntu `postgresql-16-pgvector`，RHEL 系 `pgvector_16`，或从源码编译。然后在目标库执行 `CREATE EXTENSION vector`。 |

`CREATE EXTENSION` 需要相应权限，而应用角色通常不是超级用户，**建议由 DBA 预先在目标库建好扩展**，再让应用跑迁移。

> 注意区分：Python 包 `pgvector.sqlalchemy`（已随 `requirements.txt` 安装，让 SQLAlchemy 能表达向量类型）与服务端扩展是两件不同的事——只有后者提供 `vector` 类型、距离运算与 HNSW 索引。

## 测试与安全检查

```powershell
.venv\Scripts\python -B -m pytest -q -rs -p no:cacheprovider tests
node --check web/admin.js
.venv\Scripts\python -m pip check
```

测试覆盖查询/管理隔离、OIDC、RBAC、主体数据隔离、文档 ACL、上传边界、路径约束、迁移升降级、混合检索、模型重试、费用账本、日志隐私、知识治理的状态机与召回隔离、索引队列的抢占/心跳回收/重试分类/202 异步导入、导入闭包边界与 LangGraph checkpoint 断点续跑、评测门的指标计算、生产检索路径复用、`block` 阻断、越权留痕与基线回归，以及采购硬缺口信号的引用级联语义、反馈幂等、缺口弱引用（`SET NULL`）与非 PII 模板、组织同步幂等、各管理端点鉴权 403、导出 header/行/自审计/CSV BOM、与缺口 `resolve`/`dismiss` 改状态（真 CASCADE 由 CI 在 PostgreSQL 验证）。GitHub Actions 还会执行锁定依赖漏洞扫描（核心与 Worker 两份锁文件）。

安全问题请参阅 [SECURITY.md](SECURITY.md)，不要在公开 Issue 中提交密钥或企业数据。

## 当前限制

- 异步 Worker 已实现全部四种任务类型：`import` / `reindex` / `withdraw` / `evaluate`。`evaluate` 用 `POST /api/admin/evaluation/runs` 加 `"queue": true` 入队（返回 202 + job），异步跑完黄金集并把门禁结论写在评测运行记录上；只有**无法执行**时才把任务判失败（缺版本、题集不可用、触发器非法）——指标不达标是测量结果，不是任务失败。
- LangGraph 的 PostgreSQL checkpoint 路径由 `tests/test_indexing_graph_postgres.py` 覆盖：CI 拉起 `pgvector/pgvector` 服务（与 `compose.yaml` 同镜像家族，迁移 0003 需要 `vector` 扩展），断言独立 schema 建表、续跑不重复计费、以及 checkpoint 不进入应用 schema（`alembic` 看不到）。本机没有 PostgreSQL 时该模块自动跳过；SQLite 路径由 `tests/test_indexing_graph.py` 覆盖。
- 发布前评测仍在发布请求内**同步整跑**黄金题（`trigger=pre_publish`）：题集很大时发布会变慢。异步 `evaluate` 任务已可用（见上条），但**发布路径尚未改为消费已排队/已完成的评测结论**——若没有新鲜评测，是阻断发布还是就地现跑，属于单独需要拍板的取舍。
- 办公产物保存在本地目录，尚未接入对象存储、保留策略和审批发布。
- Web 前端已实现 OIDC Authorization Code + PKCE 浏览器登录：`/api/auth/oidc/start` 发起授权、`/api/auth/oidc/callback` 换码并发放会话 Cookie；端点默认走 `<issuer>/.well-known/openid-configuration` 自动发现，可用 `IT_OIDC_*_ENDPOINT` 覆盖。真实身份提供商的联调仍需部署方配置（本仓库仅用 stub IdP 覆盖单测分支，端到端联调见 `tests/test_oidc_login.py` 注释）。
- 检索重排已上线并默认开启（`rerank_enabled`，`rerank_mode=lexical`），但只对父子分块（`chunk_child_max_chars`，默认 400）生效；`api` 模式需要外部重排服务，本仓库未内置。
- 尚未在本仓库中提供 Kubernetes、云网络策略、备份恢复和可观测性部署清单。
- 外部模型与真实身份提供商需要部署方自行配置和联调。

## 项目文档

- [项目状态](PROJECT_STATUS.md)
- [企业架构](docs/enterprise-architecture.md)
- [业务结构蓝图](docs/business-structure-blueprint.md)（v1 评审稿）
- [知识治理数据模型](docs/knowledge-governance-data-model.md)（v1 评审稿）
- [采购硬缺口架构](docs/architecture-procurement-gaps.md)
- [隔离边界](docs/isolation-boundary.md)
- [文档导入与检索](docs/document-ingestion.md)
- [实施路线图](docs/implementation-roadmap.md)
- [Embedding A/B 评测证据](docs/embedding-ab/README.md)（2026-09-22，42 题黄金集 hash vs qwen）

## License

[MIT](LICENSE)
