# DocMind IT 查询助手

独立于开发 Agent 的只读 IT 查询项目。前台只提供知识查询；SQLite 数据传输与模型调配位于 `backend/`，不注册文件写入、命令执行或游戏开发工具。

## 启动

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 8020 --no-access-log
```

`requirements.txt` / `requirements-dev.txt` 保存人工维护的直接依赖；需要严格复现已验证环境时，使用 `.venv\Scripts\python -m pip install -r requirements-lock.txt`。

打开 `http://127.0.0.1:8020/`。

健康检查：

- `GET /health/live`：进程存活检查。
- `GET /health/ready`：数据库、知识文件、网页资源和模型配置就绪检查。

每个响应均带 `X-Request-ID`。应用日志默认输出 JSON，只记录请求方法、路径、状态和耗时，不记录问题正文、回答正文、查询参数、会话 ID、客户端地址或密钥。

## 边界

- `assistant/`：只读知识检索。
- `backend/database.py`：SQLite 查询记录传输与会话隔离。
- `backend/config.py`：强类型配置、`.env` 加载和启动校验。
- `backend/logging_config.py`：带请求 ID 的结构化日志。
- `backend/models.py`：统一模型路由；默认使用确定性知识回答，可通过环境变量配置本地或云端模型画像。
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
