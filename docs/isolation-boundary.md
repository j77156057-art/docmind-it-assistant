# 与开发 Agent 的隔离边界

## 1. 目录与仓库

| 项目 | 仓库边界 | 职责 |
|---|---|---|
| 开发 Agent | 独立仓库 | 代码问答与开发工具 |
| IT 查询助手 | 当前仓库 | 企业 IT 知识查询与受控管理 |

两个项目不得通过相对路径、Git submodule、运行时 `PYTHONPATH` 或共享状态目录互相导入。

## 2. 允许迁移的能力

允许“复制后独立维护”的能力：

- 模型供应商元数据、上下文窗口和路由规则；
- Token 用量、单价与费用计算；
- 无副作用的模型重试、超时和降级策略；
- 普通文档分块、Embedding 和只读检索接口；
- 元数据 Trace、质量评测和回归门；
- 安全 Markdown 展示和通用视觉规范。
- 管理服务中的结构化 DOCX、PDF、PPTX、XLSX 渲染；必须固定输出目录、禁止任意脚本和进程执行。
- 在**非查询进程**（`ingestion/`、`worker/`）中使用 LangGraph 进行导入任务编排，
  并使用 LangSmith 进行**脱敏后**的任务追踪；查询进程不得加载上述框架。

迁移代码必须删除开发项目依赖，并在 IT 项目中拥有自己的测试、配置和版本历史。

### 2.1 编排框架的准入条件（批次 3 起生效）

LangGraph / LangSmith 属于"允许但受限"的依赖，必须同时满足：

1. **只出现在非查询进程**：`worker/` 与 `ingestion/` 可以导入；`app.py`、`assistant/`、`backend/`
   不得出现在其 import 闭包内（由 §5 的闭包测试证明，而不是靠人工评审）。
2. **业务状态仍归本项目所有**：任务可见性、重试与审计读的是 `ingestion_jobs` 与
   `document_versions`；框架 checkpoint 只保存续跑所需的内部状态，任何服务都不得读取它。
3. **checkpoint 不得写入应用 schema**：写入会让 `alembic check` 报出未知表并使迁移历史失真。
   默认使用独立 SQLite 文件（`IT_INGESTION_CHECKPOINT_PATH`），PostgreSQL 下使用独立 schema。
4. **依赖隔离**：LangGraph 及其 32 个传递依赖位于 `requirements-worker.txt`，
   查询与管理部署只安装 `requirements.txt`（依赖集合零变化）。
5. **LangSmith 默认关闭**：代码不得设置 `LANGSMITH_TRACING` / `LANGCHAIN_TRACING` 等环境变量；
   启用只能是运维的显式动作，且必须先完成脱敏评审（禁止上报正文、问题、回答与密钥）。

## 3. 禁止进入查询服务的能力

| 禁止项 | 代表模块或接口 | 原因 |
|---|---|---|
| Agent 工具执行 | `agent`、`tools`、`orchestrator` | 扩大行为权限与攻击面 |
| 项目文件访问 | `workbench_fs`、`projects`、`regions` | 可能读取或修改开发仓库 |
| 进程执行 | `subprocess`、`os.system`、`python_exec` | 查询产品不需要执行权限 |
| Git 操作 | Git 命令和回滚接口 | 与 IT 查询职责无关 |
| 游戏和引擎 | `game_workbench`、`engine_adapters` | 属于开发工作台 |
| Python 热加载 | 可执行 Hook / Skill 脚本 | 等同于服务端任意代码执行 |
| 任意联网工具 | Web 抓取、无白名单 HTTP | 可能泄露企业查询与知识内容 |
| 编排与追踪框架进入查询进程 | `langgraph`、`langchain_core`、`langsmith` | 查询进程只做检索，引入编排框架会扩大行为权限与攻击面，并让查询进程获得出站 Trace 能力 |

## 4. 运行隔离

- 使用不同虚拟环境或容器镜像；不得复用开发 Agent 的运行目录。
- 使用不同端口、数据库、对象存储 Bucket、日志和备份策略。
- 使用不同模型凭据；即使供应商相同，也使用独立服务账号和预算。
- IT 查询容器根文件系统只读；管理容器仅开放知识导入与 `IT_ARTIFACT_OUTPUT_PATH` 专用写入挂载。
- 生产网络策略默认拒绝出站，仅允许批准的模型与基础设施地址。
- `app.py` 查询进程不暴露文档上传；`ingestion.cli` 仅由后台管理员或 Worker 身份运行。
- `worker` 是独立进程与独立服务身份：只消费任务队列、读取原始文件、写入文档版本与 checkpoint，
  不与查询进程共享运行目录，也不注册任何 HTTP 路由。
- 办公产物创建、列表和下载只由独立 `admin_app.py` 提供，查询进程不导入渲染模块或注册产物路由。
- 生产中查询角色只授予已发布知识读取及查询账本写入权限，导入角色单独授予文档版本写入权限。

## 5. 自动守卫

`tests/test_isolation_boundary.py` 以两种机制守卫边界：

**（1）导入闭包检查（主守卫）**：从 `app.py` 出发递归解析 import，只要闭包内出现
`langgraph`、`langgraph_sdk`、`langchain_core`、`langchain_protocol` 或 `langsmith` 即失败，
并打印命中的文件路径与完整闭包。这比"按文件名黑名单"更强：无论导入写在哪个文件、
是否被中间模块包装，都能被捕获。

**（2）名单与文本检查（补充）**：

- 拒绝导入开发 Agent、工作台、游戏或进程执行模块，扫描范围已扩展至 `ingestion/` 与 `worker/`；
- 拒绝调用 `os.system`、`os.popen`、`subprocess.run` 或 `subprocess.Popen`；
- 拒绝在查询侧使用 `import_module` / `__import__` / `sys.modules[...]` 动态加载上述框架；
- 拒绝 `worker/` 反向导入 `app.py`、`admin_app.py` 或 `assistant/`；
- 拒绝任何源码设置 `LANGSMITH_TRACING` / `LANGCHAIN_TRACING` / `LANGSMITH_API_KEY`。

后续应在 CI 增加依赖树检查、容器权限检查和出站网络测试。

## 6. 变更审查清单

每次合并前确认：

- [ ] 新依赖是否只用于查询、检索、存储、安全或观测？
- [ ] 若新依赖属于编排/追踪类框架：是否只落在 `worker/`、是否通过闭包检查、是否默认关闭追踪？
- [ ] 是否新增了文件写入、命令执行、Git 或任意网络能力？
- [ ] 用户输入是否可能进入日志、模型外部端点或错误信息？
- [x] 文档 ACL 是否在检索阶段生效？
- [ ] API Key 是否只报告“已配置”，不返回原文？
- [ ] 新能力是否有隔离测试和拒绝路径测试？
