# DocMind IT 查询助手

独立于开发 Agent 的只读 IT 查询项目。前台只提供知识查询；数据库、混合检索与模型调配位于 `backend/`，不注册文件写入、命令执行或游戏开发工具。文档导入通过独立后台命令完成，不暴露在查询 API。

## 启动

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 8020 --no-access-log
```

`requirements.txt` / `requirements-dev.txt` 保存人工维护的直接依赖；需要严格复现已验证环境时，使用 `.venv\Scripts\python -m pip install -r requirements-lock.txt`。

打开 `http://127.0.0.1:8020/`。

健康检查：

- `GET /health/live`：进程存活检查。
- `GET /health/ready`：数据库、知识文件、网页资源和模型配置就绪检查。

每个响应均带 `X-Request-ID`。应用日志默认输出 JSON，只记录请求方法、路径、状态和耗时，不记录问题正文、回答正文、查询参数、会话 ID、客户端地址或密钥。

## 文档导入与混合检索

支持 Markdown、TXT、PDF、DOCX。导入过程执行大小/页数/文本长度限制、SHA-256 去重、不可变版本、标题感知分块和向量化；新版本成功发布后旧版本自动标记为 `superseded`。普通查询 API 没有上传能力。

```powershell
.venv\Scripts\python -m ingestion.cli import D:\docs\vpn-manual.pdf --title "VPN 手册" --source-key "it/vpn-manual"
.venv\Scripts\python -m ingestion.cli list
```

查询时使用全文 BM25 和向量余弦召回，再通过 RRF 融合排序；Embedding 服务失败时降级为全文检索。引用包含文档、版本、章节、页码或块编号。`IT_EMBEDDING_MODE=hash` 只用于本地开发，生产环境强制使用真实 `provider` 模式。

详细配置、发布流程和恢复说明见 [`docs/document-ingestion.md`](docs/document-ingestion.md)。

## 真实模型网关与费用账本

当知识资料不足且 `IT_MODEL_MODE=local|cloud` 时，服务会调用所选供应商的 OpenAI 兼容 `/chat/completions` 接口。云端密钥只从进程环境或未提交的 `.env` 读取；可通过 `IT_CLOUD_BASE_URL`、`IT_LOCAL_BASE_URL` 或 `IT_CUSTOM_BASE_URL` 覆盖地址。

网关配置包括超时、最多重试次数、最大输出 Token 和温度。供应商返回的权威 usage 会逐次写入 `model_usage_ledger`，包括失败重试、延迟、供应商请求 ID、当时单价和费用快照，但不保存问题/回答正文、密钥或供应商错误正文。价格未知时费用为 `null`，不会误显示为免费。

- `GET /api/usage/summary?session_id=...`：本会话 Token 与费用汇总。
- `GET /api/usage/ledger?session_id=...`：本会话逐次模型调用账本。

查询页面右侧显示本会话的调用次数、Token 和费用。内置知识回答不调用模型，因此不会产生模型 Token 账目。

## PostgreSQL 与迁移

生产环境强制使用 `IT_DATABASE_URL=postgresql+psycopg://...`，连接串不会出现在状态接口或日志。应用不会在 PostgreSQL 中自动建表，部署前必须执行：

```powershell
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m alembic current
```

本地没有现成 PostgreSQL 时，可先用 `docker compose up -d postgres` 启动带 pgvector 的数据库服务。托管 PostgreSQL 必须预先允许 `vector` 扩展。升级前应备份；降级命令与注意事项见 [`migrations/README.md`](migrations/README.md)。SQLite 仅保留给本地演示和自动化测试，生产配置会拒绝启动。

## 边界

- `assistant/`：只读知识检索。
- `backend/database.py`：SQLAlchemy Repository、连接池和数据库健康检查。
- `backend/db_models.py`：与 Alembic 共用的数据模型元数据。
- `migrations/`：可升级、可降级的数据库版本记录。
- `backend/config.py`：强类型配置、`.env` 加载和启动校验。
- `backend/logging_config.py`：带请求 ID 的结构化日志。
- `backend/models.py`：统一模型路由；默认使用确定性知识回答，可通过环境变量配置本地或云端模型画像。
- `backend/model_gateway.py`：真实模型调用、有限重试和权威 usage 提取。
- `backend/embeddings.py`：真实 Embeddings 接口与本地确定性测试向量。
- `backend/retrieval.py`：全文、向量、RRF 融合与全文降级。
- `ingestion/`：与查询 API 分离的解析、分块和版本化导入命令。
- `backend/providers.py`：从开发项目隔离出的模型供应商目录与上下文能力，不依赖 Agent 或开发工具。
- `backend/pricing.py`：独立 Token 单价与费用计算；本地模型默认费用为零。
- 不包含开发问答、项目文件访问、Shell、Git 或游戏工具。
- `tests/test_isolation_boundary.py`：自动阻止开发 Agent、工作台、Git/Shell 和进程执行依赖进入查询服务。

## 模型环境变量

- `IT_MODEL_MODE=knowledge|local|cloud`
- `IT_LOCAL_PROVIDER=ollama|llamacpp`
- `IT_LOCAL_MODEL=<model>`
- `IT_CLOUD_PROVIDER=qwen|deepseek|kimi|zhipu|siliconflow|openai|custom`
- `IT_CLOUD_MODEL=<model>`

运行状态接口 `/api/runtime/model` 只返回模型名称、上下文窗口、计价信息与 Key 是否已配置，绝不返回 Key 原文。

## 项目文档

- [`docs/enterprise-architecture.md`](docs/enterprise-architecture.md)：企业级目标架构、数据流和非功能指标。
- [`docs/isolation-boundary.md`](docs/isolation-boundary.md)：与开发 Agent 的隔离规则、允许与禁止能力。
- [`docs/implementation-roadmap.md`](docs/implementation-roadmap.md)：分阶段实施表、验收条件和风险控制。
- [`HANDOFF.md`](HANDOFF.md)：当前状态、验证基线和下一步交接事项。
