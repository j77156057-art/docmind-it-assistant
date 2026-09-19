# DocMind IT 查询助手：企业级架构方案

## 1. 产品定位

DocMind IT 查询助手是面向企业员工的只读知识查询系统。它根据用户身份检索其有权访问的 IT 知识，生成带出处的回答；资料不足时明确拒答并提示需要补充的信息。

系统不是开发 Agent，不执行命令、不修改终端、不访问工程仓库，也不代替工单系统实施变更。终端用户查询面与知识管理面必须分开部署和授权。

## 2. 设计原则

| 原则 | 要求 |
|---|---|
| 证据优先 | 回答必须基于授权知识片段，并返回文档版本、章节和位置 |
| 最小权限 | 查询服务不具备 Shell、Git、项目文件写入或任意网络访问能力 |
| 身份前置 | 在检索前完成认证和文档 ACL 过滤，不能在生成答案后再脱敏 |
| 独立运行 | 不导入、挂载或运行 `rag-agent` 中的开发工具和状态目录 |
| 可观测 | 记录请求编号、耗时、路由、Token、费用和结果状态，不默认保存正文 |
| 可降级 | 模型不可用时仍可返回确定性知识片段或安全拒答 |

## 3. 目标架构

```text
员工浏览器
    │ OIDC / SSO
    ▼
查询 API ── 限流 / 审计 / 输入策略
    │
    ├── 权限上下文 ── 用户、部门、角色、知识密级
    │
    ├── 检索服务 ── 全文召回 + 向量召回 + 重排
    │       │
    │       ├── PostgreSQL / pgvector
    │       └── 文档元数据与版本
    │
    ├── 模型网关 ── 本地 / 云端路由、超时、降级、预算
    │
    └── 引用校验 ── 无证据拒答、越权引用拦截

管理后台（独立身份与端口）
    │
    ├── 文档导入任务
    ├── 解析、分块、去重、向量化
    └── 发布、回滚、ACL 与质量评测
```

## 4. 模块职责

| 模块 | 当前实现 | 企业级目标 |
|---|---|---|
| `assistant/` | 版本化知识检索、引用和安全拒答 | ACL 后查询编排与引用持久化 |
| `backend/database.py` | PostgreSQL/SQLite Repository、pgvector 混合检索 | 数据保留、分区与租户隔离 |
| `backend/models.py` | 模型路由与真实调用 | 熔断、预算和自动降级 |
| `backend/providers.py` | 独立供应商目录 | 配置中心、密钥引用和供应商准入 |
| `backend/pricing.py` | Token 单价与费用计算 | 版本化价格表、预算和成本告警 |
| `web/` | 只读查询页面 | SSO、引用展开、反馈、历史与无障碍支持 |
| `ingestion/` | 独立解析、去重、版本、分块和向量化命令 | 异步 Worker、审批发布、回滚和管理审计 |

## 5. 核心数据模型

| 实体 | 关键字段 |
|---|---|
| User | `id`、`tenant_id`、`department`、`roles`、`status` |
| Document | `id`、`source`、`classification`、`owner`、`current_version` |
| DocumentVersion | `document_id`、`hash`、`published_at`、`status` |
| Chunk | `version_id`、`text`、`embedding`、`section`、`page`、`acl` |
| Query | `request_id`、`user_id_hash`、`route`、`evidence_state`、`latency` |
| Citation | `query_id`、`chunk_id`、`score`、`display_location` |
| ModelUsage | `query_id`、`provider`、`model`、`input_tokens`、`output_tokens`、`cost` |
| AuditEvent | `actor`、`action`、`target`、`result`、`timestamp` |

## 6. 模型与 Token 口径

- Token 表示模型或模拟器处理的上下文规模，不天然等于费用。
- 本地模型和内置确定性检索可以有 Token 用量，但默认费用为零。
- `mock` 用量是启发式估算，用于验证预算、裁剪和性能，不得计入真实 API 账单。
- 云端费用必须使用经管理员批准、带生效日期的价格表计算。
- 生产记录应区分 `measured`、`estimated` 和 `test` 三类用量来源。

## 7. 安全控制

1. 身份：OIDC/OAuth2，短期访问令牌，服务端校验受众、签发方和过期时间。
2. 权限：文档 ACL 在召回查询中强制执行，禁止先检索后过滤。
3. 输入：限制长度、文件类型与请求频率；检测提示词注入和凭据内容。
4. 输出：只允许返回授权证据，敏感字段按策略遮盖，无证据时拒答。
5. 网络：查询服务只访问数据库、对象存储和批准的模型端点。
6. 密钥：通过企业密钥管理系统注入，不写入仓库、日志或状态接口。
7. 审计：管理操作全量记录；查询日志默认不保存问题与回答正文。

## 8. 非功能指标

| 指标 | 建议目标 |
|---|---:|
| API 可用性 | ≥ 99.9% |
| 检索 P95 | ≤ 1 秒 |
| 流式首字 P95 | ≤ 2 秒 |
| 完整回答 P95 | ≤ 8 秒 |
| 授权过滤正确率 | 100% |
| 引用可追溯率 | 100% |
| 严重依赖漏洞 | 0 |
| 关键模块测试覆盖率 | ≥ 80% |
| 建议 RPO / RTO | ≤ 24 小时 / ≤ 4 小时 |

## 9. 部署建议

- 开发：单进程 FastAPI + SQLite，仅用于本地演示。
- 测试：Docker Compose + PostgreSQL/pgvector + 独立对象存储。
- 生产：无状态查询实例、多副本 Worker、托管数据库、统一入口网关。
- 查询端、管理端和后台 Worker 使用不同服务身份与网络策略。
- 发布采用数据库迁移、镜像扫描、自动评测和灰度回滚门禁。
