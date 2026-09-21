# Project Status

更新日期：2026-09-20

## 已完成

- 查询服务与管理服务拥有独立入口和能力边界。
- PostgreSQL/SQLite Repository 与五个可升降级 Alembic 迁移。
- Markdown、TXT、PDF、DOCX 版本化导入和全文 + 向量混合检索。
- OIDC Bearer Token 校验、`viewer/auditor/admin` RBAC 和匿名化主体隔离。
- 用户、组、角色级文档 ACL，并在召回前执行过滤。
- 管理后台文档导入、ACL 编辑、审计、健康状态和运行时模型切换。
- DOCX、PDF、PPTX、XLSX 结构化生成、回读验证和受控下载。
- 模型重试、Token/费用账本、动态模型路由和密钥脱敏。
- 知识治理：版本状态机（`queued/processing/staged/indexed/rejected/withdrawn/superseded/failed`）、能力制治理角色、审批发布与职责分离、作废与回滚、审核预览与 `document_version_reviews` 留痕。
- PowerShell 本地启停脚本与 Docker Compose 本地 PostgreSQL 环境。
- 自动测试、依赖漏洞扫描和 Dependabot 更新。

## 安全边界

- 查询服务不注册上传、ACL 写入、办公产物生成或命令执行路由。
- `development` 认证只允许配置为回环地址；生产配置强制 OIDC。
- API Key 只从进程环境或未提交的 `.env` 读取。
- 日志和审计不保存问题正文、回答正文、Token 原文或供应商错误正文。
- 生成文件固定在专用目录，并拒绝路径穿越和电子表格公式注入。
- 日志字段是白名单（标识符、枚举、计数、布尔），问题正文、回答正文与文档内容无法通过 `log_event` 的额外字段进入日志；白名单有专门测试固定。

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


## 后续工作

1. 将同步导入改为异步 Worker，并增加审批发布流程。
1. 补充 `reindex` / `withdraw` / `evaluate` 任务类型（含异步评测），并为可重试失败增加持久化退避（`next_attempt_at`）。
3. 增加浏览器 OIDC Authorization Code + PKCE 登录。
4. 接入对象存储、保留策略、审计导出和引用持久化。
5. 增加检索质量数据集运营（黄金题评审流程）、性能压测和生产可观测性。
6. 引入检索重排与知识域模型（域 B），支持按范围隔离审核与评测。
7. 提供 Kubernetes、网络策略、备份恢复和灰度发布参考部署。
