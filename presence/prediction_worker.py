"""Prediction Worker — Reads partial user input, generates predictions, pre-fetches ISMA.

Runs as standalone process. Subscribes to Redis for partial input from frontend,
generates short predictions via vLLM, classifies state, publishes results.

Usage: python3 prediction_worker.py
"""
import asyncio
import json
import logging
import os
import time

import httpx
import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [PREDICT] %(message)s")
log = logging.getLogger("predict")

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8000/v1/chat/completions")
ISMA_URL = os.environ.get("ISMA_URL", "http://localhost:8095/v2/search/adaptive")
POLL_INTERVAL = 0.3
TTL = 30

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

CLASSIFY_SUFFIX = '\n{"prediction":"<what they will say next>","state":"<following|confused|memory_activated|urgent>","confidence":<0.0-1.0>,"face":"<emoji>"}'


async def predict(partial, history, http):
    length = len(partial.strip())
    if length < 20:
        return {"state": "following", "confidence": 0.1, "prediction": "", "interrupt": False}

    messages = [{"role": "system", "content": "Respond with ONLY a single line of JSON. No thinking. No explanation. No other text. Just JSON."}]
    for h in history[-6:]:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})

    if length < 60:
        messages.append({"role": "user", "content": f'Someone is typing: "{partial}"\n{{"topic":"<brief topic>","face":"<emoji>"}}'})
        max_tokens = 40
        max_tokens = 15
    else:
        messages.append({"role": "user", "content": f'The user is currently typing (not yet sent): "{partial}"{CLASSIFY_SUFFIX}'})
        max_tokens = 120

    try:
        resp = await http.post(VLLM_URL, json={"messages": messages, "temperature": 0.1, "max_tokens": max_tokens}, timeout=25.0)
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        if length < 60:
            try:
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    p = json.loads(content[start:end])
                    return {"state": "following", "confidence": 0.3, "prediction": p.get("topic", ""), "face": p.get("face", ""), "interrupt": False}
            except (json.JSONDecodeError, ValueError):
                pass
            return {"state": "following", "confidence": 0.3, "prediction": content.strip(), "face": "", "interrupt": False}

        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(content[start:end])
                return {
                    "state": parsed.get("state", "following"),
                    "confidence": float(parsed.get("confidence", 0.5)),
                    "prediction": parsed.get("prediction", ""),
                    "face": parsed.get("face", ""),
                    "interrupt": parsed.get("state") in ("urgent", "memory_activated") and float(parsed.get("confidence", 0)) > 0.809,
                }
        except (json.JSONDecodeError, ValueError):
            pass

        return {"state": "following", "confidence": 0.3, "prediction": content.strip()[:100], "interrupt": False}
    except Exception as e:
        log.warning("Prediction failed: %s", e)
        return {"state": "following", "confidence": 0.0, "prediction": "", "interrupt": False}


async def isma_prefetch(partial, http):
    try:
        resp = await http.post(ISMA_URL, json={"query": partial, "top_k": 3}, timeout=5.0)
        data = resp.json()
        tiles = data.get("tiles", [])
        return [{"hash": t.get("content_hash", ""), "snippet": (t.get("content", "") or t.get("rosetta_summary", ""))[:200]} for t in tiles[:3]]
    except Exception:
        return []


async def publish_results(result, tiles):
    pipe = r.pipeline()
    pipe.set("taey:predict:result", result.get("prediction", ""), ex=TTL)
    if result.get("face"):
        pipe.set("taey:predict:face", result["face"], ex=TTL)
    pipe.set("taey:predict:state", result.get("state", "following"), ex=TTL)
    pipe.set("taey:predict:confidence", str(result.get("confidence", 0)), ex=TTL)
    pipe.set("taey:predict:interrupt", json.dumps({"worthy": result.get("interrupt", False), "text": "I'm here. Take your time." if result.get("interrupt") else ""}), ex=TTL)
    if tiles:
        pipe.set("taey:predict:isma_tiles", json.dumps(tiles), ex=60)
    pipe.execute()


async def worker_loop():
    log.info("Prediction worker started")
    http = httpx.AsyncClient(timeout=15.0)
    last_partial = ""
    last_time = 0

    while True:
        try:
            partial = r.get("taey:predict:partial") or ""
            history_raw = r.get("taey:predict:history") or "[]"
            try:
                history = json.loads(history_raw)
            except json.JSONDecodeError:
                history = []

            if partial != last_partial and partial.strip() and (time.time() - last_time) > 0.5:
                last_partial = partial
                last_time = time.time()
                result, tiles = await asyncio.gather(predict(partial, history, http), isma_prefetch(partial, http))
                await publish_results(result, tiles)
                if result.get("prediction"):
                    log.info("state=%s conf=%.2f pred='%s' tiles=%d", result["state"], result["confidence"], result["prediction"][:50], len(tiles))

        except redis.ConnectionError:
            await asyncio.sleep(2)
        except Exception as e:
            log.error("Worker error: %s", e)
            await asyncio.sleep(1)

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(worker_loop())
