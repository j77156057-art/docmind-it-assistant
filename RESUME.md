# DocMind IT Assistant — 简历战果一页纸

> 用途：把项目里"可量化、可验证"的工程成果集中成可直接贴简历 / 面试复述的素材。所有数字均来自仓库实测与 CI artifact，commit 可查（见 `README.md`「项目亮点」）。

## 一句话定位

安全型 RAG 企业知识助手（FastAPI + PostgreSQL/pgvector + Vue3），带可量化质量门禁、可验证架构决策与真实迁移历史——不是 demo 玩具。

## 简历 Bullet（复制即用）

1. 将 80 个阻塞式 `async def` 路由 handler 重构为 `def`，把 SQLAlchemy 调用下沉到 anyio 默认 40 线程 worker 池，消除事件循环阻塞；全程由 **232 passed / 4 skipped** 的 pytest 门禁兜底，零回归。（`d2339ef`）
2. 用结构化慢日志 + 并发有界性测试定位真正的吞吐瓶颈：证明 **anyio 40 线程上限 > PostgreSQL 连接池 15（5+10）**，故连接池而非线程池才是约束；配套慢日志分析器固化"池饱和 vs 慢 SQL"判据。（`fd32aa3` `a54c20c` `263d588`）
3. 建立**复用生产检索路径**的发布质量门禁（42 题黄金集，recall@k / 引用命中率 / 拒答正确率），CI `--gate-mode block` 强制设门；修复词法重排器 IDF 缺陷后，本仓库 CI 通过题数 **16/21 → 20/21**，引用命中率 **0.7143 → 0.8571**。（`5126e19` `25564c7` `2217f54`）
4. 将"一身二职"的认证盐拆分为 **3 把独立可轮换密钥**（主体哈希 / 会话 JWT / 供应商凭证），并配套凭证轮换脚本，消除"轮换一把密钥同时废掉两种密文"的爆炸半径。（`4d61e4c` `de32022`）
5. 落地 **20 个可升降级 Alembic 迁移**（混合检索 pgvector、ACL、知识治理、保留期、字段加密等）；修复仅在 CI 真实 PostgreSQL 下暴露的迁移非法参数 bug。（`ca9db6f`）
6. 设计**两套 CI 护栏**防止"发绿但没真跑"：collect-only 断言每个测试模块至少贡献 1 例；扫描运行日志的 `fatal exception` / `access violation` / `Segmentation fault`。（`scripts/check_suite_completeness.py` `scripts/check_run_log.py`）

## 面试深挖要点

- **单变量实验纪律**：评测严格复用生产检索路径（非另写 SQL），且一次只改一个变量（如先修 IDF 再测），避免"换 embedding 无用"这类被混淆变量误导的结论。
- **池瓶颈的因果排除法**：先用品红线（anyio 40 > PG 15）判定池是真瓶颈，单调递减线程数在池不变时零收益——不是"看起来相关"就调参。
- **崩溃仍发绿的真实案例**：全量 pytest 曾打出 `access violation` 却仍报 passed + exit 0，故加日志扫描护栏把"崩了还绿"挡在门禁外。

## 诚实边界（面试别过度承诺）

- 连接池"容量 / 吞吐"**仍未在生产流量下验证**（仅验证有界性）；决策 C 待真 PG 流量喂 `analyze_slow_logs.py` 后定尺寸。
- 评测黄金集目前 21–42 题量级，非大规模；rerank 仅父子分块生效，`api` 模式需外部服务。
- 项目为本地可运行工程化演示，非已上线生产部署。

## 技术栈

Python 3.13 · FastAPI / Starlette / anyio · SQLAlchemy · PostgreSQL + pgvector · Alembic · Vue3 · OIDC / RBAC · Fernet 字段加密 · pytest（+ CI 护栏）
