# 数据库迁移

生产环境只允许 Alembic 管理 PostgreSQL 表结构。部署顺序：先备份，再执行升级，确认 `/health/ready` 后发布应用。

```powershell
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m alembic current
```

回退前必须确认新版本尚未写入不可兼容数据：

```powershell
.venv\Scripts\python -m alembic downgrade -1
```

连接串来自 `IT_DATABASE_URL`；不要写入 `alembic.ini` 或提交 `.env`。

当前版本：

- `20260920_0001`：查询历史表。
- `20260920_0002`：逐次模型调用、Token、价格快照与费用账本。
- `20260920_0003`：文档、不可变版本、知识块、pgvector 索引和 Embedding 用量归属。
- `20260920_0004`：查询身份归属、文档 ACL 与安全审计事件。
- `20260920_0005`：单例运行时模型配置，用于管理端动态切换模型。
