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
