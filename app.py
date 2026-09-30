"""SonicTag Studio 入口：创建 FastAPI 实例、挂载静态资源与路由、启动服务。"""
import os
import sys
import threading
import webbrowser
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.routes import router

app = FastAPI(title="SonicTag Studio - Ultimate Edition")


def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


app.mount("/static", StaticFiles(directory=get_resource_path("static")), name="static")
app.include_router(router)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html_path = get_resource_path("static/index.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="未找到 static/index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:8000")


if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)
