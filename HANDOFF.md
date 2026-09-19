# DocMind IT 查询助手交接文档

更新日期：2026-09-20

## 项目身份

- 本地目录：`D:\WorkBuddy\docmind-it-assistant`
- GitHub：`https://github.com/j77156057-art/docmind-it-assistant`
- 仓库可见性：Private
- 默认分支：`main`
- 本地演示端口：`8020`

## 已完成

- 与开发 Agent 分为独立文件夹、Git 仓库和运行入口。
- 查询服务只提供知识查询、历史和模型状态接口。
- 已建立模型供应商目录、上下文窗口和 Token 计价模块。
- 模型状态只返回 Key 是否已配置，不返回 Key 内容。
- 已加入源码隔离测试，禁止引入开发 Agent、Shell、Git 和进程执行能力。
- 已完成企业架构、隔离边界和实施路线图文档。
- 已创建项目独立 `.venv`，直接依赖固定版本，并提供完整 `requirements-lock.txt`。
- 已实现强类型配置、`.env.example`、存活/就绪检查和隐私收敛的 JSON 日志。
- 已引入 PostgreSQL、SQLAlchemy Repository 和 Alembic；生产环境禁止 SQLite 和自动建表。
- 已接入 OpenAI 兼容真实模型网关，并实现逐尝试 Token/费用账本和前端会话汇总。

## 当前接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 查询页面 |
| POST | `/api/query` | 只读知识查询 |
| GET | `/api/history` | 当前会话查询历史 |
| GET | `/api/runtime/model` | 模型路由、上下文和计价状态 |
| GET | `/api/usage/summary` | 会话 Token 与费用汇总 |
| GET | `/api/usage/ledger` | 会话逐次模型调用账本 |
| GET | `/health/live` | 进程存活检查 |
| GET | `/health/ready` | 数据库、知识、网页和模型就绪检查 |

## 当前限制

- 只支持本地 `knowledge.md`，尚未实现企业文档导入。
- 当前首个迁移仅包含查询历史表，引用、用量和审计表仍待后续迁移。
- 尚未配置真实生产供应商密钥，也未在当前机器执行外部供应商联调。
- 尚未接入企业 SSO、RBAC 和文档 ACL。
- 前端尚未提供独立管理后台、反馈和引用详情面板。

## 验证基线

```powershell
D:\WorkBuddy\docmind-it-assistant\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

当前基线：28 项测试通过，覆盖查询、真实网关协议、失败重试、Token/费用账本、路由、计价、隔离边界、配置校验、健康检查、迁移升降级与漂移检查、请求 ID 和日志隐私。

启动示例：

```powershell
.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 8020 --no-access-log
```

## 下一步

按 `docs/implementation-roadmap.md` 从 P0 工程底座开始：

1. 建立引用与审计数据表；
2. 为用量接口加入企业身份和权限；
3. 接入文档导入链路与正式检索。

不要在当前演示结构上直接加入开发工具，也不要从 `rag-agent` 运行时导入模块。
