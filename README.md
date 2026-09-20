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
- **隐私日志**：日志不记录问题正文、回答正文、查询参数、会话 ID、客户端地址或密钥。

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

## Docker 本地环境

Docker Compose 会启动 PostgreSQL/pgvector、迁移任务、查询服务和管理服务，宿主机端口仍只绑定 `127.0.0.1`：

```powershell
docker compose up -d --build
docker compose ps
docker compose down
```

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
IT_AUTH_SUBJECT_SALT=<至少 32 字符的随机值>
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

## 模型与密钥

回答策略可在管理后台选择：`knowledge_first`（默认，命中后直接返回知识库）、`generative_first`（先按 ACL 检索，再交给模型组织客服回答）或 `hybrid`（简单问题直接返回，复杂问题生成式回答）。生成式提示词只包含当前用户可见的检索片段，并保留来源引用；模型不可用时会降级为确定性知识答案或安全拒答。知识证据不足时，可路由到 Ollama、llama.cpp 或 OpenAI 兼容云供应商。云端密钥可以来自进程环境、未提交的 `.env`，或管理后台加密保存的运行时凭据；接口响应、页面与审计日志不会回显明文。

云端模式可直接在“系统状态 → 模型配置”填写供应商、模型和 API Key。系统会先发起一次真实的短请求验证连接，再把密钥用 `IT_AUTH_SUBJECT_SALT` 派生的密钥加密保存；页面和接口只返回“已配置”，不会回显明文。切勿随意更换 `IT_AUTH_SUBJECT_SALT`，否则已保存密钥将无法解密；生产环境更推荐将密钥放入部署平台的 Secret Manager 或环境变量。

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

## 测试与安全检查

```powershell
.venv\Scripts\python -B -m unittest discover -s tests -v
node --check web/admin.js
.venv\Scripts\python -m pip check
```

测试覆盖查询/管理隔离、OIDC、RBAC、主体数据隔离、文档 ACL、上传边界、路径约束、迁移升降级、混合检索、模型重试、费用账本和日志隐私。GitHub Actions 还会执行锁定依赖漏洞扫描。

安全问题请参阅 [SECURITY.md](SECURITY.md)，不要在公开 Issue 中提交密钥或企业数据。

## 当前限制

- 管理端文档导入仍是同步请求，尚未拆成异步 Worker。
- 办公产物保存在本地目录，尚未接入对象存储、保留策略和审批发布。
- Web 前端尚未实现 OIDC Authorization Code + PKCE 登录，当前生产入口面向 Bearer Token 客户端或身份网关。
- 尚未在本仓库中提供 Kubernetes、云网络策略、备份恢复和可观测性部署清单。
- 外部模型与真实身份提供商需要部署方自行配置和联调。

## 项目文档

- [项目状态](PROJECT_STATUS.md)
- [企业架构](docs/enterprise-architecture.md)
- [隔离边界](docs/isolation-boundary.md)
- [文档导入与检索](docs/document-ingestion.md)
- [实施路线图](docs/implementation-roadmap.md)

## License

[MIT](LICENSE)
