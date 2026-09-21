# 知识供给治理：数据模型与迁移设计（v1 评审稿）

> **状态：批次 1 已实现。** 本文档是 [业务结构蓝图](business-structure-blueprint.md) 的配套实现设计。
> 与实现之间的偏差记录在蓝图 §12「实施状态」；其中一条影响本文档表格：`document_version_reviews`
> 实际落地时增加了 `is_override` 布尔列（见 §4.2）。
> 文中 `database.py:NNN` 一类行号是**设计时基线**，实现后已发生位移，请以符号名为准。

## 1. 设计目标与不变量

| # | 不变量 | 现状证据 | 本设计如何保证 |
|---:|---|---|---|
| 1 | 召回只认 `status='indexed'` | `database.py:430`、`:462`、`:376`、`:341` | **不改这四处 SQL**，新增的状态全部落在 `indexed` 之前或之后 |
| 2 | ACL 在召回前生效 | `database.py:431-438`、`:463-470` | 不新增任何"先召回后过滤"路径；新增的审核预览刻意绕开 ACL 但强制能力校验 + 审计 |
| 3 | 审计 append-only | `db_models.py:193-205` | 审批历史独立成 `document_version_reviews`，只插入不更新 |
| 4 | 单租户 | 本轮决策 | **不引入 `tenant_id`**（代价见 §6.6） |
| 5 | SQLite 与 PostgreSQL 双方言可迁移 | `migrations/versions/20260920_0003:94-118`、`0004:19-31` | 新增列/约束全部走 `op.batch_alter_table` 兼容路径，逐级升降级测试覆盖 |

## 2. 现状快照（改动前）

现有状态值全集（来自 `database.py:237-303`）：`pending`、`indexed`、`superseded`、`failed`。

重要事实：**`document_versions.status` 今天没有 CHECK 约束**（见 `migrations/versions/20260920_0003:36-51`
与 `backend/db_models.py:109`，两者都只是普通 `String(24)` 列）。所以状态是"约定值"而非"受约束值"——
本设计补上 CHECK 约束，这意味着必须先回填再约束。

其他相关事实：

- `document_versions` 唯一约束：`uq_document_versions_number (document_id, version)` 与
  `uq_document_versions_hash (document_id, content_sha256)`（`0003:49-50`）。
- `document_chunks.document_version_id` 外键 `ondelete="CASCADE"`（`0003:74-76`），
  `model_usage_ledger.document_version_id` 同样级联（`0003:111-114`）。
- 原文件按 `data/sources/<document_id>/v<version>-<filename>` 落盘（`document_sources.py:32-34`）。

## 3. 变更总览

| 对象 | 类型 | 用途 |
|---|---|---|
| `document_versions` | 扩展列 + CHECK | 承载状态机当前态与治理指针 |
| `document_version_reviews` | 新表（append-only） | 提交/审核/发布/作废/回滚/强推的完整留痕 |
| `ingestion_jobs` | 新表 | 业务队列：可见、可重试、可取消、可限流 |
| `evaluation_cases` | 新表 | 黄金题集 |
| `evaluation_runs` | 新表 | 一次评测执行的指标与门禁结论 |
| `evaluation_case_results` | 新表 | 逐题结果 |

## 4. 详细设计

### 4.1 `document_versions` 扩展

新增列：

| 列 | 类型 | 空 | 说明 |
|---|---|---|---|
| `submitted_by_subject_id` | `String(64)` | 是 | 提交人（用于职责分离校验） |
| `submitted_at` | `DateTime(tz)` | 是 | 提交时间（待审列表排序） |
| `indexed_at` | `DateTime(tz)` | 是 | 索引完成（进入 `staged`）的时间；与 `published_at` 区分 |
| `published_by_subject_id` | `String(64)` | 是 | 发布人 |
| `withdrawn_at` | `DateTime(tz)` | 是 | 作废时间 |
| `withdrawn_reason` | `String(512)` | 是 | 作废/回滚原因（强制填写） |
| `superseded_by_version_id` | `Integer` | 是 | 自引用外键，指向取代它的版本，支持回滚链溯源 |

`published_at` 已存在（`0003:46`），沿用。

新增 CHECK 约束（回填之后再建）：

```sql
CHECK (status IN ('queued','processing','staged','indexed',
                  'rejected','withdrawn','superseded','failed'))
```

新增索引：

```sql
CREATE INDEX ix_document_versions_status_submitted
    ON document_versions (status, submitted_at);
```

理由：待审列表是治理后台最热的查询（`WHERE status='staged' ORDER BY submitted_at`），
现有 `ix_document_versions_status`（`0003:81`）只能过滤不能排序。

**不新增** `current_review_state` 之类的冗余列——当前态由 `status` 单点表达，避免双写不一致。

### 4.2 `document_version_reviews`（append-only）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `Integer` PK | |
| `document_version_id` | `Integer` FK → `document_versions.id` `CASCADE`，索引 | |
| `action` | `String(24)` | `submit`/`reopen`/`approve`/`reject`/`publish`/`withdraw`/`rollback`/`override_gate` |
| `from_status` / `to_status` | `String(24)` | 迁移前后状态，供审计复盘 |
| `actor_subject_id` | `String(64)`，索引 | 匿名化主体（沿用 `subject_identifier`） |
| `comment` | `String(1000)` | 审核意见 / 驳回原因 / 强推理由 |
| `is_override` | `Boolean` 默认 false | 是否由管理员越权开关完成（实现阶段补充，避免越权只靠注释表达） |
| `request_id` | `String(128)`，索引 | 与日志、审计、模型账本关联 |
| `created_at` | `DateTime(tz)`，索引 | |

```sql
CHECK (action IN ('submit','reopen','approve','reject','publish',
                  'withdraw','rollback','override_gate'))
```

**与 `audit_events` 的分工**（不要合并）：

| 表 | 语义 | 读者 |
|---|---|---|
| `audit_events` | 安全事件流（谁访问了原文件、谁改了 ACL） | 审计员、合规 |
| `document_version_reviews` | 业务审批链（这一版为什么能上线） | 知识 Owner、审核、发布 |

合并会让"查一次审批历史"变成在大杂烩事件流里猜动作名。

### 4.3 `ingestion_jobs`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `Integer` PK | |
| `job_type` | `String(16)` | `import`/`reindex`/`withdraw`/`evaluate` |
| `status` | `String(16)` | `queued`/`running`/`succeeded`/`failed`/`cancelled` |
| `priority` | `Integer` 默认 100 | 数值小优先 |
| `document_id` / `version_id` | `Integer` FK，可空，索引 | 关联业务对象 |
| `payload` | `JSON` | 任务参数（**禁止写入文件正文**） |
| `attempts` / `max_attempts` | `Integer` | 重试控制 |
| `last_error_code` | `String(64)` | 对齐现有 `error_code` 命名 |
| `locked_by` | `String(64)`，索引 | Worker 标识 |
| `locked_at` / `heartbeat_at` | `DateTime(tz)` | 僵尸任务回收 |
| `created_by_subject_id` | `String(64)` | 任务发起人 |
| `request_id` | `String(128)` | 链路关联 |
| `created_at` / `started_at` / `finished_at` | `DateTime(tz)` | |

防重复投递（实现采用普通唯一约束 `uq_ingestion_jobs_target (job_type, version_id)`，见下方说明）：

```sql
CREATE UNIQUE INDEX uq_ingestion_jobs_active
    ON ingestion_jobs (job_type, version_id)
    WHERE status IN ('queued','running');
```

**实现偏差（批次 2 落地时修改）**：设计原本使用"部分唯一索引"，实现改为**普通唯一约束**
`(job_type, version_id)`，并把"重新投递"定义为**复用同一行**（重置 attempts 与错误码）。
理由：

1. 部分唯一索引只在 `queued/running` 时生效，那么一个 `failed` 行不会阻止新行插入，
   重试就会不断堆积历史任务行；
2. 普通唯一约束让"一个版本一个任务身份"成为硬约束，重试天然变成重排队；
3. 普通唯一约束在 SQLite 与 PostgreSQL 的反射行为一致，`alembic check` 保持无差异。

索引：

```sql
CREATE INDEX ix_ingestion_jobs_claim ON ingestion_jobs (status, priority, id);
CREATE INDEX ix_ingestion_jobs_created_at ON ingestion_jobs (created_at);
```

⚠️ **方言约束**：SQLite 没有 `SELECT ... FOR UPDATE SKIP LOCKED`。
`config.py:160-163` 已经在生产环境强制 PostgreSQL，因此：
生产走真正的抢占式并发；开发/测试走单 Worker 乐观认领
（`UPDATE ingestion_jobs SET status='running' WHERE id=:id AND status='queued'`，按受影响行数判断归属）。

### 4.4 `evaluation_cases`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `Integer` PK | |
| `case_key` | `String(64)` 唯一 | 稳定标识，便于跨环境对齐 |
| `question` | `Text` | 评测问题（**这是测试数据，不是用户数据**） |
| `expect_refusal` | `Boolean` 默认 false | 是否属于"应拒答"题 |
| `expected_document_key` | `String(512)` 可空 | 期望命中的 `documents.source_key` |
| `expected_heading` | `String(512)` 可空 | 期望命中的章节标题 |
| `tags` | `String(256)` 可空 | 域分类（VPN / 邮箱 / 权限…） |
| `active` | `Boolean` 默认 true | 停用而不删除 |
| `created_by_subject_id` / `created_at` / `updated_at` | | |

### 4.5 `evaluation_runs`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `Integer` PK | |
| `trigger` | `String(16)` | `manual`/`pre_publish`/`scheduled` |
| `document_version_id` | FK 可空 | 被门禁的版本（`manual` 全量评测时为空） |
| `status` | `String(16)` | `running`/`succeeded`/`failed` |
| `gate_mode` | `String(8)` | `off`/`warn`/`block`（记录当时的配置，便于事后解释） |
| `gate_result` | `String(12)` 可空 | `pass`/`warn`/`block`/`overridden`；`NULL` 表示门禁关闭（实现阶段补充） |
| `gate_reason` | `String(512)` | 阻断/告警原因（实现阶段补充：没有原因就无法解释"为什么这一版被阻断"） |
| `total_cases` / `passed_cases` / `failed_cases` | `Integer` | |
| `recall_at_k` / `citation_accuracy` / `refusal_accuracy` | `Numeric(6,4)` 可空 | |
| `baseline_run_id` | FK 自引用可空 | 回归比较基线 |
| `created_by_subject_id` / `request_id` / `started_at` / `finished_at` / `error_code` | | |

### 4.6 `evaluation_case_results`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `Integer` PK | |
| `run_id` | FK → `evaluation_runs.id` `CASCADE` | |
| `case_id` | FK → `evaluation_cases.id` `RESTRICT` | 题被引用时不允许删 |
| `retrieved` / `citation_ok` / `refusal_ok` | `Boolean` 可空 | |
| `matched_rank` | `Integer` 可空 | 期望文档的命中排名 |
| `latency_ms` | `Integer` | |
| `detail` | `JSON` 可空 | **禁止写入回答正文**（沿用隐私口径，只写状态与计数） |

唯一约束 `(run_id, case_id)`。

## 5. 迁移批次

命名沿用现有 `YYYYMMDD_NNNN_描述` 风格（见 `migrations/versions/`）。

| 迁移 | 内容 | upgrade 要点 | downgrade 要点 |
|---|---|---|---|
| `20260921_0008_version_governance` | 状态机 + 治理列 + reviews 表 | ① 回填 `pending → queued`；② 为历史 `indexed` 版本补一条 `action='publish'`、`actor='system:backfill'`、`comment='历史数据回填，免审批'` 的记录；③ 加列；④ 最后加 CHECK | ① 先归一状态（见下方映射）；② 删表删列 |
| `20260921_0009_ingestion_jobs` | 业务队列表 | 建表 + 唯一约束 + 抢占索引 | 删表 |
| `20260921_0010_evaluation_gate` | 评测门三表 | 建表 + 外键 + 唯一约束 + CHECK | 删表（先备份质量历史） |
| `20260921_0010_evaluation_gate` | 三张评测表 | 建表 + 外键 + 唯一约束 | 删表 |

### 5.1 降级时的状态归一映射（**关键安全点**）

旧代码只认 `pending/indexed/superseded/failed`，而召回一律 `WHERE status='indexed'`。
若降级时把未审核内容映射成 `indexed`，**等于绕过审批直接把草稿推上线**。

| 治理状态 | 降级后 | 理由 |
|---|---|---|
| `queued`、`processing` | `pending` | 语义接近（未发布、可被重复导入逻辑复用） |
| `staged` | `pending` | **绝不能是 `indexed`**：未审核内容不得进入检索 |
| `rejected` | `failed` | 旧代码无"被拒"概念，归入失败且不可检索 |
| `withdrawn` | `superseded` | 均表示"曾经发布、现已下线" |
| `indexed` | `indexed` | 不变 |

降级前必须先备份：`document_version_reviews` 与 `ingestion_jobs` 一旦删表，审批证据链即丢失。

### 5.2 SQLite 批量重建的注意事项

`document_versions` 被 `document_chunks`（`0003:74-76`）与 `model_usage_ledger`（`0003:111-114`）
以 `ON DELETE CASCADE` 外键引用。SQLite 的 `batch_alter_table` 会重建整张表，
因此该迁移必须：

1. 显式使用 `with op.batch_alter_table("document_versions") as batch:`（与 `0004:19-31` 同风格）；
2. 在 `batch` 内声明新增列、约束、索引与自引用外键，避免重建后丢失；
3. 升降级测试必须跑在**已有数据**的库上，而不只是空库——
   否则外键与 CASCADE 问题会在生产首次迁移时才暴露。

实际实现中，自引用外键 `fk_document_versions_superseded_by_version` **两种方言都建立**
（原计划仅 PostgreSQL）。这样 SQLite 与 PostgreSQL 的 schema 完全一致，`alembic check`
不会报出差异；代价是 SQLite 迁移需要重建 `document_versions`。

### 5.3 现有迁移风格参考

- 方言分支写法：`0003:21-23`、`0003:94-118`。
- `batch_alter_table` 加列加索引：`0004:19-31`。
- 索引命名：`ix_<table>_<column>`；约束命名：`ck_*` / `uq_*` / `fk_*`。

## 6. 兼容性与影响面

### 6.1 必须一起改的函数

| 函数 | 现状 | 改动 |
|---|---|---|
| `begin_document_import`（`database.py:205`） | 新版本置 `pending` | 置 `queued`；写 `submitted_*`；插入 `submit` 评审记录 |
| 同上，重复判定（`database.py:237`） | `existing.status in {"indexed", "pending"}` | 改为"在途或已发布"集合 `{"indexed","queued","processing","staged"}` |
| 同上，复用已存在版本（`database.py:242-245`） | 直接重置为 `pending` | **删除该行为**：`rejected`/`failed` 版本不得被覆盖（见 §6.3） |
| `complete_document_import`（`database.py:265`） | 直接置 `indexed` + 写 `published_at` + supersede 旧版本 | 拆成 `finalize_document_indexing`（`publish=False` → `staged`，写 `indexed_at`）与 `publish_document_version`（→ `indexed`，写 `published_at`，supersede 旧版本，插 `publish` 记录）；`complete_document_import` 保留为 `publish=True` 的薄包装，避免破坏 CLI 与既有测试 |
| `fail_document_import`（`database.py:299`） | 置 `failed` | 增加"是否可重试"判断：可重试→回 `queued` 且 `attempts+1`；不可重试→`failed` |
| `list_documents`（`database.py:306`） | 只返回基础列 | 增加治理列（`submitted_by_subject_id`、`submitted_at`、`reviewed` 摘要） |
| `admin_app.py:525` 导入端点 | 同步跑完整流水线 | 异步时返回 `202 + job_id`；`IT_GOVERNANCE_MODE=direct` 且 Worker 关闭时保持同步行为 |

`has_indexed_chunks`（`:337`）、`accessible_document_outline`（`:346`）、
`hybrid_search`（`:413`）、`lexical_search`（`:419`）：**不动**。

### 6.2 审核预览需要新路径

`accessible_document_outline`（`database.py:376`）硬编码 `status='indexed'`，
所以 `staged` 版本对所有人都不可见——这正是我们要的。但审核者必须能看到内容才能审核。

设计：新增管理侧端点读取**指定 `version_id`** 的分块：

| 端点 | 能力 | 说明 |
|---|---|---|
| `GET /api/admin/documents/{id}/versions/{version}/preview` | `document.review` | 返回该版本分块摘要；**每次都写审计** |

约束：该端点刻意不套 ACL（审核者可能不是该文档的 ACL 对象），因此必须以
"能力 + 单版本 + 强制审计"三重限制收口，且**只返回被审版本**，不提供跨文档遍历。

### 6.3 唯一哈希约束与"驳回后重提"的冲突（必须处理）

`uq_document_versions_hash (document_id, content_sha256)`（`0003:50`）意味着
**同一文档不可能存在两个内容相同的版本行**。因此"驳回后一律新建版本"在内容未变时无法执行。

最终策略：

| 情形 | 处理 |
|---|---|
| 内容已修改 | 新建版本号（`version+1`），保留被拒版本作为证据 |
| 内容未变（同 hash）且状态为 `rejected`/`failed` | **复用原行**，置回 `queued`，并写 `reopen` 评审记录（保留原驳回意见） |

这同时修掉了 `database.py:242-245` 今天"静默重置"的行为——改为显式 `reopen` 留痕。

### 6.4 ⚠️ 已发现的安全缺口：导入会改写 `access_scope`

`begin_document_import`（`database.py:226-232`）在文档已存在时，会直接
更新 `title` / `mime_type` / **`access_scope`** / `classification`：

```python
if normalized_scope is not None:
    document.access_scope = normalized_scope   # ← 这里
```

今天这个端点是 `Depends(admin)`（`admin_app.py:532`），所以尚可接受。
但**一旦引入 `knowledge_editor` 角色并允许其导入**，编辑只要用一个已存在的 `source_key`
再传 `access_scope=public`，就能把一份受限文档改成全员可见——这是一条权限提升路径。

强制控制（批次 1 必须实现）：

1. 导入**不得**修改已发布文档的 `access_scope` / `classification`；
2. 访问范围变更必须走独立入口，要求 `acl.write` 能力，并写审计（`set_document_acl` 已有此约束，`database.py:605`）；
3. 新增测试：编辑角色导入同名 `source_key` 且传 `access_scope=public` → 被拒，文档原范围不变。

### 6.5 配置项新增

> **批次归属（避免误读为已实现）**：批次 1 落地了 `IT_GOVERNANCE_MODE`、
> `IT_GOVERNANCE_REQUIRE_SEPARATION_OF_DUTIES`、`IT_GOVERNANCE_ALLOW_ADMIN_OVERRIDE`
> 与 `IT_LOCAL_ROLES`；批次 2 落地了全部 `IT_INGESTION_*`；批次 3 追加
> `IT_INGESTION_ENGINE` 与 `IT_INGESTION_CHECKPOINT_PATH`；批次 4 落地了全部 `IT_EVAL_*`
> 并新增 `IT_EVAL_ALLOW_OVERRIDE`（越权放行评测门的独立开关，与职责分离开关解耦）。
> 下表中的 `IT_LANGSMITH_*` **仍未实现且属有意为之**——批次 3 只引入 LangGraph，
> LangSmith 按决策 5 保持关闭（代码禁止设置任何 `LANGSMITH_TRACING` 环境变量）。
> 另有一条实现偏差：`citation_accuracy` 的分母是"需要命中的题数"而非"命中数"，
> 以保证 `citation_accuracy ≤ recall_at_k` 恒成立（详见蓝图 §12 批次 4）。

| 配置 | 默认 | 说明 |
|---|---|---|
| `IT_GOVERNANCE_MODE` | `direct` | `direct`＝保持现状（索引完成即发布）；`review`＝走审批 |
| `IT_GOVERNANCE_REQUIRE_SEPARATION_OF_DUTIES` | `true` | 提交人 ≠ 审核人 |
| `IT_GOVERNANCE_ALLOW_ADMIN_OVERRIDE` | `false` | 允许 admin 单人完成（仍留痕） |
| `IT_INGESTION_WORKER_ENABLED` | `false` | 关闭时导入保持同步 |
| `IT_INGESTION_WORKER_ID` | 空 | 空则自动生成 |
| `IT_INGESTION_POLL_SECONDS` | `2` | 轮询间隔 |
| `IT_INGESTION_MAX_ATTEMPTS` | `3` | 重试上限 |
| `IT_INGESTION_JOB_TIMEOUT_SECONDS` | `600` | 僵尸回收阈值 |
| `IT_INGESTION_HEARTBEAT_SECONDS` | `30` | 心跳间隔 |
| `IT_EVAL_GATE_MODE` | `warn` | `off`/`warn`/`block`（批次 4 已实现） |
| `IT_EVAL_ALLOW_OVERRIDE` | `false` | 是否允许越权绕过阻断（实现阶段新增，独立于职责分离开关） |
| `IT_EVAL_MIN_RECALL` | `0.8` | |
| `IT_EVAL_MIN_CITATION_ACCURACY` | `0.9` | |
| `IT_EVAL_MAX_REGRESSION` | `0.05` | 相对基线的允许退化 |
| `IT_LANGSMITH_ENABLED` | `false` | 默认关闭 |
| `IT_LANGSMITH_API_URL` | 空 | 开启时必须是 `https://` |
| `IT_LANGSMITH_PROJECT` | `docmind-ingestion` | |
| `IT_LANGSMITH_HIDE_CONTENT` | `true` | 断言载荷不含正文 |

新增生产校验追加到 `config.py` 的 `require_postgresql_in_production` 中：

- `IT_LANGSMITH_ENABLED=true` → 要求 `IT_LANGSMITH_API_URL` 为 `https://` 且 `IT_LANGSMITH_HIDE_CONTENT=true`（批次 3）。
- 已实现的一条独立校验：`IT_INGESTION_HEARTBEAT_SECONDS` 必须小于 `IT_INGESTION_JOB_TIMEOUT_SECONDS`，
  否则回收逻辑会把仍在工作的任务判为遗弃。

**已废弃的原始设想**：设计原计划在 `production` + `IT_GOVERNANCE_MODE=review` 时强制
`IT_INGESTION_WORKER_ENABLED=true`。实现时取消该约束——同步导入在 `review` 模式下依然成立
（索引在请求内完成，终点是 `staged`），强制启用 Worker 会无理由地阻断一种合法部署。
队列是否停滞改由 `/health/ready` 的 `ingestion.stalled` 诊断字段暴露。

### 6.6 单租户决策的代价（记录，不实现）

不引入 `tenant_id` 让本轮改造最小，但代价必须写清楚，避免将来误判成本：

- 将来多租户需给 `documents`、`document_acl`、`ingestion_jobs`、`evaluation_*`、`queries`
  全部加列，并重建所有唯一约束（`uq_document_versions_number`、`uq_document_versions_hash`、
  `uq_document_acl_principal`、`uq_ingestion_jobs_active`）为含租户的复合约束；
- 检索 SQL 的 ACL 子句要加租户过滤（`database.py:431-438`、`:463-470`），并补跨租户越权测试。

## 7. 测试计划

新增测试文件建议：`tests/test_knowledge_governance.py`、`tests/test_ingestion_jobs.py`、
`tests/test_evaluation_gate.py`，并扩展 `tests/test_database_migrations.py` 与
`tests/test_isolation_boundary.py`。沿用现有 `unittest` 风格（`tests/` 下无 pytest 依赖）。

| 类别 | 断言 |
|---|---|
| 迁移 | `0007 → 0008 → 0009 → 0010` 逐级升级；`0010 → 0009 → 0008 → 0007` 逐级降级；**在有数据的库上执行** |
| 检索隔离 | `staged`/`rejected`/`withdrawn` 版本在 `hybrid_search`、`lexical_search`、`accessible_document_outline` 中**均不可见**（最高优先级断言） |
| 状态机 | 非法迁移被拒：`staged → indexed` 无发布能力、`rejected → indexed` 跳步、`withdrawn → staged` |
| 职责分离 | 同一主体提交 + 审批 → `separation_of_duties_violation` |
| 权限提升 | 编辑导入同名 `source_key` + `access_scope=public` → 被拒且文档范围不变（§6.4） |
| 唯一性 | 同 hash 在 `rejected` 后重提 → 复用行 + `reopen` 记录，不产生重复版本 |
| 队列 | 重复投递幂等；超 `max_attempts` → `failed`；心跳超时 → 回收为 `queued` |
| 评测门 | `block` 阻断发布；`override` 必留痕；评测调用走 `hybrid_search`（不复制 SQL）——`tests/test_evaluation_gate.py` 已实现，含 `hybrid_search` 调用计数断言与基线回归用例 |
| 边界 | `app.py` import 闭包内无 `langgraph`/`langchain_core`/`langsmith`（见蓝图 §8） |
| 隐私 | `IT_LANGSMITH_ENABLED=true` 时上报载荷不含正文；`ingestion_jobs.payload` 不含正文 |

## 8. 未决问题

与蓝图 §10 的六个决策点一一对应，其中影响本文档结构的三条：

1. **决策点 1（`indexed` 语义）**：若改为 `published`，本文档 §1 不变量 1 与 §6.1 的"不动四处 SQL"全部失效。
2. **决策点 2（admin 是否自带发布权）**：影响 §4.2 中 `override_gate` 与 §6.5 的
   `IT_GOVERNANCE_ALLOW_ADMIN_OVERRIDE` 语义。
3. **决策点 3（`IT_GOVERNANCE_MODE` 默认值）**：若默认 `review`，现有
   `test_document_ingestion_retrieval` 中"导入后立即可检索"的断言需要改写，且本地演示流程变长。
