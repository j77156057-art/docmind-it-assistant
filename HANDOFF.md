# DocMind IT 查询助手交接文档

更新日期：2026-09-19

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

## 当前接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 查询页面 |
| POST | `/api/query` | 只读知识查询 |
| GET | `/api/history` | 当前会话查询历史 |
| GET | `/api/runtime/model` | 模型路由、上下文和计价状态 |

## 当前限制

- 只支持本地 `knowledge.md`，尚未实现企业文档导入。
- SQLite 仅适合本地演示，尚未迁移 PostgreSQL。
- 模型路由已具备元数据和计价，尚未执行真实模型调用。
- 尚未接入企业 SSO、RBAC 和文档 ACL。
- 前端尚未提供独立管理后台、反馈和引用详情面板。

## 验证基线

```powershell
D:\WorkBuddy\rag-agent\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

当前基线：8 项测试通过，包括查询、路由、计价和隔离边界。

启动示例：

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8020
```

## 下一步

按 `docs/implementation-roadmap.md` 从 P0 工程底座开始：

1. 建立正式分层和配置对象；
2. 添加健康检查、请求 ID 和结构化日志；
3. 引入 PostgreSQL 与 Alembic；
4. 再接企业身份和文档导入链路。

不要在当前演示结构上直接加入开发工具，也不要从 `rag-agent` 运行时导入模块。
