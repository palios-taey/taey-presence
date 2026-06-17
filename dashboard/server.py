"""taey-presence dashboard — the integrating web layer.

A thin FastAPI app that bridges a browser to the Redis keys the presence engine
and soma daemon read/write. It does NOT run inference itself; it is the seam
between the UI and the running daemons.

- POST /push    {partial, history}  -> writes presence:partial / presence:history
- GET  /state                       -> reads presence:{face,prediction,interrupt,memory}
                                        + the soma agent:state:vector
- GET  /                            -> serves static/index.html (the live UI)

No auth — Redis is on your own trusted network (see repo README Security).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="taey-presence")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
_r = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)  # no auth


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.post("/push")
async def push(req: Request):
    """Frontend pushes the user's current partial input (+ optional history)."""
    body = await req.json()
    partial = str(body.get("partial", ""))
    pipe = _r.pipeline()
    pipe.set("presence:partial", partial, ex=60)
    if "history" in body:
        pipe.set("presence:history", json.dumps(body["history"]), ex=300)
    await pipe.execute()
    return JSONResponse({"ok": True})


async def _get_json(key):
    raw = await _r.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


@app.get("/state")
async def state():
    """Everything the UI renders: face, ghost prediction, interrupt, memory, soma."""
    return JSONResponse({
        "face": await _get_json("presence:face"),
        "prediction": await _get_json("presence:prediction"),
        "interrupt": await _get_json("presence:interrupt"),
        "memory": await _get_json("presence:memory"),
        "soma": await _get_json("agent:state:vector"),
    })


def main():
    import uvicorn
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")  # loopback default — see Security
    port = int(os.environ.get("DASHBOARD_PORT", "8700"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
