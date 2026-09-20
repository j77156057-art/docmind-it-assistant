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
- PowerShell 本地启停脚本与 Docker Compose 本地 PostgreSQL 环境。
- 自动测试、依赖漏洞扫描和 Dependabot 更新。

## 安全边界

- 查询服务不注册上传、ACL 写入、办公产物生成或命令执行路由。
- `development` 认证只允许配置为回环地址；生产配置强制 OIDC。
- API Key 只从进程环境或未提交的 `.env` 读取。
- 日志和审计不保存问题正文、回答正文、Token 原文或供应商错误正文。
- 生成文件固定在专用目录，并拒绝路径穿越和电子表格公式注入。

## 验证基线

```powershell
.venv\Scripts\python -B -m unittest discover -s tests -v
node --check web/admin.js
.venv\Scripts\python -m pip check
```

当前测试覆盖查询/管理隔离、OIDC、RBAC、主体数据隔离、文档 ACL、文档解析、版本去重、混合检索、降级、模型网关、Token/费用账本、配置、迁移和日志隐私。

## 后续工作

1. 将同步导入改为异步 Worker，并增加审批发布流程。
2. 增加浏览器 OIDC Authorization Code + PKCE 登录。
3. 接入对象存储、保留策略、审计导出和引用持久化。
4. 增加检索质量数据集、性能压测和生产可观测性。
5. 提供 Kubernetes、网络策略、备份恢复和灰度发布参考部署。
