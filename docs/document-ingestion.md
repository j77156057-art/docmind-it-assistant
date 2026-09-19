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

`source-key` 是稳定文档身份。同一身份和相同 SHA-256 不重复建索引；内容变化生成新版本。只有向量化和分块全部成功后新版本才变为 `indexed`，此前版本随后标记为 `superseded`。失败版本保留错误码，下一次同内容导入可以重试。

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
