# 文档导入与混合检索运维说明

## 运行边界

查询服务不提供上传接口。`ingestion.cli` 应由后台管理员、作业系统或独立 Worker 身份运行，不能授予普通查询用户。生产环境建议为查询与导入配置不同数据库角色和网络策略。

## 支持格式与限制

| 格式 | 提取规则 |
|---|---|
| Markdown | 按标题保留章节 |
| TXT | UTF-8 / UTF-8 BOM 文本 |
| PDF | 按页提取，并保留页码；不支持需密码解密的文件 |
| DOCX | 按 Heading 样式保留章节 |

默认限制为 20 MiB、500 页、200 万字符。可通过 `IT_DOCUMENT_MAX_BYTES`、`IT_DOCUMENT_MAX_PAGES`、`IT_DOCUMENT_MAX_CHARACTERS` 调整。解析器不会执行文档中的宏、脚本或外部命令。

## 导入与版本

```powershell
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m ingestion.cli import D:\docs\manual.docx --title "IT 操作手册" --source-key "it/manual"
.venv\Scripts\python -m ingestion.cli list
```

`source-key` 是稳定文档身份。同一身份和相同 SHA-256 不重复建索引；内容变化生成新版本。版本状态依次为 `queued → processing → staged → indexed`，**只有 `indexed` 参与召回**；失败版本保留错误码，下一次同内容导入可以重试。

## 知识治理（审批发布）

默认 `IT_GOVERNANCE_MODE=direct`：索引完成即发布，行为与历史版本一致。切换为 `review` 后：

1. 导入完成后版本停在 `staged`，其分块已入库但**任何召回路径都取不到**；
2. `knowledge_reviewer` 通过 `POST /api/admin/documents/{id}/versions/{v}/review` 通过或驳回（驳回必须填写意见）；
3. 通过只代表"授权发布"，不会自动上线；`knowledge_publisher` 再调用 `POST .../publish` 才把状态改为 `indexed`，同时把上一个已发布版本标记为 `superseded`；
4. `POST .../withdraw` 需要填写原因，版本立即退出检索，但原文件、分块与审批记录都保留；`POST .../rollback` 可把历史版本重新置为已发布，且**不重新向量化**。

治理动作的全部留痕在 `document_version_reviews`（只插入、不更新），与安全审计 `audit_events` 分开：前者回答"这一版为什么能上线"，后者回答"谁在什么时候动了什么"。

关键约束：

- `admin` **不自动拥有**审核与发布能力。小团队可设置 `IT_GOVERNANCE_ALLOW_ADMIN_OVERRIDE=true` 并显式勾选越权开关，动作会带 `is_override` 标记。
- 默认强制职责分离（提交人不能审核自己提交的版本），可用 `IT_GOVERNANCE_REQUIRE_SEPARATION_OF_DUTIES` 关闭。
- 治理角色不会进入 ACL 解析，因此授予审核角色**不会**顺带扩大该用户的文档可见范围。
- 导入接口不能修改已存在文档的 `access_scope`；越权修改会被拒绝（`403`），范围调整只能走 ACL 接口。

CLI 导入同样尊重 `IT_GOVERNANCE_MODE`：`review` 模式下命令执行完只到 `staged`，`--access-scope` 与既有文档不一致时会直接报错退出。

## 异步索引（Worker）

运维要点：

| 现象 | 位置 | 处理 |
|---|---|---|
| 任务长期停留在 `queued` | `/api/admin/ingestion/jobs`、`/health/ready` 的 `ingestion.stale` | Worker 未运行或已退出；启动 Worker 后会自动消费 |
| `job_type_unsupported` | 任务视图 | 该任务类型尚未实现（当前仅 `import`） |

`ingestion/cli.py` 的同步导入始终保留：Worker 子系统不可用时，管理员仍可导入知识，这是刻意保留的降级通道。

## Embedding 配置

本地开发默认使用 `IT_EMBEDDING_MODE=hash`，它只验证导入与检索流程，不应作为生产语义向量。切换模式或模型后，应重新导入所有当前文档以重建同一向量空间。生产配置会强制要求：

```env
IT_EMBEDDING_MODE=provider
IT_EMBEDDING_PROVIDER=qwen
IT_EMBEDDING_MODEL=text-embedding-v3
IT_EMBEDDING_DIMENSION=1024
DASHSCOPE_API_KEY=...
```

供应商必须提供 OpenAI 兼容 `/embeddings` 接口并返回 1024 维向量。Embedding usage 会写入模型用量账本，但未配置经审批的 Embedding 单价时费用保持未知，不按 0 元处理。

## 检索与降级

PostgreSQL 使用 `tsvector`/GIN 全文索引和 pgvector/HNSW 余弦索引，两个候选集合通过 RRF 融合。SQLite 使用等价的本地 BM25 和余弦实现，仅供开发测试。Embedding 查询失败时退回全文检索；全文和向量均无可信命中时继续走既有知识文件或安全拒答。

## 发布与恢复

升级前备份 PostgreSQL，并确认数据库允许 `CREATE EXTENSION vector`，或由 DBA 预先安装扩展。迁移后执行：

```powershell
.venv\Scripts\python -m alembic current
.venv\Scripts\python -m alembic check
```

降级 `20260920_0003` 会删除文档索引和导入产生的 Embedding 用量记录。执行前必须备份，且不应在生产高峰期直接降级。

降级 `20260921_0008` 会删除 `document_version_reviews`（审批证据链）与治理列，并把新状态归一为旧词汇：`queued`/`processing`/`staged` → `pending`，`rejected` → `failed`，`withdrawn` → `superseded`。**不会**把未审核内容映射为 `indexed`，避免降级动作本身把草稿推上线。执行前必须备份审批记录。
