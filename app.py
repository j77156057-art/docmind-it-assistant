from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from assistant import ITQueryService
from backend import ModelRouter, QueryDatabase


ROOT = os.path.dirname(os.path.abspath(__file__))
database = QueryDatabase(os.getenv("IT_DATABASE_PATH") or os.path.join(ROOT, "data", "queries.db"))
models = ModelRouter()
service = ITQueryService(os.path.join(ROOT, "knowledge.md"), database, models)


@asynccontextmanager
async def lifespan(_app):
    database.initialize()
    yield


app = FastAPI(title="DocMind IT Assistant", lifespan=lifespan)


class QueryReq(BaseModel):
    session_id: str = "default"
    question: str = ""


@app.get("/")
async def index():
    return FileResponse(os.path.join(ROOT, "web", "index.html"))


@app.post("/api/query")
async def query(req: QueryReq):
    try:
        return {"ok": True, **service.query(req.session_id, req.question)}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/history")
async def history(session_id: str = "default", limit: int = 20):
    return {"ok": True, "items": database.history(session_id, limit)}


@app.get("/api/runtime/model")
async def model_status():
    return {"ok": True, **models.status()}

