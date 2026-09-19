# DocMind IT 查询助手

独立于开发 Agent 的只读 IT 查询项目。前台只提供知识查询；SQLite 数据传输与模型调配位于 `backend/`，不注册文件写入、命令执行或游戏开发工具。

## 启动

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 8020
```

打开 `http://127.0.0.1:8020/`。

## 边界

- `assistant/`：只读知识检索。
- `backend/database.py`：SQLite 查询记录传输与会话隔离。
- `backend/models.py`：统一模型路由；默认使用确定性知识回答，可通过环境变量配置本地或云端模型画像。
- 不包含开发问答、项目文件访问、Shell、Git 或游戏工具。

