"""Local web UI for the C: space report. It listens on 127.0.0.1 only."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.service import Analyzer

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
analyzer = Analyzer()


class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")


app = FastAPI(title="C 盘空间分析", default_response_class=UTF8JSONResponse)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def local_only(request, call_next):
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1"}:
        return JSONResponse({"ok": False, "message": "仅允许本机访问"}, status_code=403)
    return await call_next(request)


class CleanRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    confirm: bool = False


class FileRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)
    confirm: bool = False


class SoftwareItem(BaseModel):
    id: str = ""
    name: str = ""
    install_location: str = ""


class UninstallRequest(BaseModel):
    items: list[SoftwareItem] = Field(default_factory=list)
    confirm: bool = False


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/api/state")
def state():
    return analyzer.snapshot()


@app.post("/api/scan/start")
def start_scan():
    result = analyzer.start_scan()
    return UTF8JSONResponse(result, status_code=200 if result.get("ok") else 409)


@app.post("/api/scan/stop")
def stop_scan():
    return analyzer.stop_scan()


@app.post("/api/caches/clean")
def clean_caches(body: CleanRequest):
    if len(body.ids) > 40:
        return UTF8JSONResponse({"ok": False, "message": "一次选择太多"}, status_code=400)
    if not body.confirm:
        return analyzer.preview_clean(body.ids)
    result = analyzer.start_clean(body.ids)
    code = 200 if result.get("ok") or result.get("rejected") else 409
    return UTF8JSONResponse(result, status_code=code)


@app.post("/api/files/delete")
def delete_files(body: FileRequest):
    if len(body.paths) > 100:
        return UTF8JSONResponse({"ok": False, "message": "一次最多删除 100 个文件"}, status_code=400)
    if not body.confirm:
        return analyzer.preview_files(body.paths)
    return analyzer.delete_files(body.paths)


@app.post("/api/software/uninstall")
def uninstall_software(body: UninstallRequest):
    items = [item.model_dump() for item in body.items]
    if len(items) > 20:
        return UTF8JSONResponse({"ok": False, "message": "一次最多卸载 20 个软件"}, status_code=400)
    if not body.confirm:
        return analyzer.preview_uninstall(items)
    return analyzer.start_uninstall(items)
