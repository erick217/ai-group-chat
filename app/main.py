from __future__ import annotations

from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import ROOT

load_dotenv(ROOT / ".env")

from .engine import Room  # noqa: E402  需要先加载 .env

app = FastAPI(title="AI 群聊")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
room = Room()


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/export")
async def export():
    md = room.export_markdown()
    name = f"群聊-{room.template.name}.md"
    return Response(md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    room.clients.add(ws)
    await ws.send_json(room.state())
    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")
            if kind == "send":
                await room.user_message(str(data.get("text", "")))
            elif kind == "continue":
                await room.continue_discussion()
            elif kind == "stop":
                await room.stop()
            elif kind == "clear":
                await room.clear()
            elif kind == "reload":
                await room.reload()
            elif kind == "settings":
                await room.update_settings(data)
            elif kind == "template":
                await room.set_template(str(data.get("key", "")))
    except WebSocketDisconnect:
        pass
    finally:
        room.clients.discard(ws)
