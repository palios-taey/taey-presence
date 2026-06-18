"""Prediction Worker — Reads partial user input, generates predictions, pre-fetches ISMA.

Runs as standalone process. Subscribes to Redis for partial input from frontend,
generates short predictions via vLLM, classifies state, publishes results.

Usage: python3 prediction_worker.py
"""
import asyncio
import json
import logging
import os
import re
import time

import httpx
import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [PREDICT] %(message)s")
log = logging.getLogger("predict")

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8000/v1/chat/completions")
# Optional model name. Leave empty for single-model servers (vLLM serves one
# model and ignores it). REQUIRED by servers that demand it (e.g. Ollama's
# /v1 — set MODEL=qwen2.5:3b or similar).
MODEL = os.environ.get("MODEL", "")
ISMA_URL = os.environ.get("ISMA_URL", "http://localhost:8095").rstrip("/")
ISMA_SEARCH_URL = f"{ISMA_URL}/v2/search/adaptive"
POLL_INTERVAL = 0.3
TTL = 30

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

CLASSIFY_SUFFIX = '\n{"prediction":"<what they will say next>","state":"<following|confused|memory_activated|urgent>","confidence":<0.0-1.0>,"face":"<emoji>"}'


def _extract_prediction_payload(content):
    if not content:
        return {}

    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}") + 1
    candidate = text[start:end].strip() if start >= 0 and end > start else ""
    repairs = []
    if candidate:
        repairs.extend([
            candidate,
            re.sub(r',\s*""\s*(?=,|})', "", candidate),
            re.sub(r",\s*([}\]])", r"\1", candidate),
        ])
        repairs.append(re.sub(r",\s*([}\]])", r"\1", repairs[-1]))

    for attempt in repairs:
        try:
            parsed = json.loads(attempt)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

    face_match = re.search(r'"face"\s*:\s*"([^"\n]+)"', text)
    topic_match = re.search(r'"topic"\s*:\s*"([^"\n]*)"', text)
    prediction_match = re.search(r'"prediction"\s*:\s*"([^"\n]*)"', text)
    state_match = re.search(r'"state"\s*:\s*"([^"\n]*)"', text)
    confidence_match = re.search(r'"confidence"\s*:\s*(-?\d+(?:\.\d+)?)', text)

    extracted = {}
    if face_match:
        extracted["face"] = face_match.group(1)
    if topic_match:
        extracted["topic"] = topic_match.group(1)
    if prediction_match:
        extracted["prediction"] = prediction_match.group(1)
    if state_match:
        extracted["state"] = state_match.group(1)
    if confidence_match:
        try:
            extracted["confidence"] = float(confidence_match.group(1))
        except ValueError:
            pass
    return extracted


def _clean_prediction_text(value):
    text = (value or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    if text.startswith("{") and text.endswith("}"):
        return ""
    return text


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
    else:
        messages.append({"role": "user", "content": f'The user is currently typing (not yet sent): "{partial}"{CLASSIFY_SUFFIX}'})
        max_tokens = 120

    try:
        resp = await http.post(VLLM_URL, json={"messages": messages, "temperature": 0.1, "max_tokens": max_tokens, **({"model": MODEL} if MODEL else {})}, timeout=25.0)
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = _extract_prediction_payload(content)

        if length < 60:
            topic = parsed.get("topic") or parsed.get("prediction") or _clean_prediction_text(content)
            return {"state": "following", "confidence": 0.3, "prediction": topic, "face": parsed.get("face", ""), "interrupt": False}

        if parsed:
            try:
                confidence = float(parsed.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            state = parsed.get("state", "following")
            prediction = parsed.get("prediction") or parsed.get("topic") or _clean_prediction_text(content)
            return {
                "state": state,
                "confidence": confidence,
                "prediction": prediction,
                "face": parsed.get("face", ""),
                "interrupt": state in ("urgent", "memory_activated") and confidence > 0.809,
            }

        return {"state": "following", "confidence": 0.3, "prediction": _clean_prediction_text(content)[:100], "interrupt": False}
    except Exception as e:
        log.warning("Prediction failed: %s", e)
        return {"state": "following", "confidence": 0.0, "prediction": "", "interrupt": False}


async def isma_prefetch(partial, http):
    try:
        resp = await http.post(ISMA_SEARCH_URL, json={"query": partial, "top_k": 3}, timeout=5.0)
        data = resp.json()
        tiles = data if isinstance(data, list) else data.get("results", data.get("tiles", []))
        return [{"hash": t.get("content_hash", ""), "snippet": (t.get("content", "") or t.get("rosetta_summary", ""))[:200]} for t in tiles[:3]]
    except Exception:
        return []


async def publish_results(result, tiles):
    prediction = _clean_prediction_text(result.get("prediction", ""))
    if not prediction:
        prediction = result.get("topic", "") or ""
    pipe = r.pipeline()
    pipe.set("taey:predict:result", prediction, ex=TTL)
    pipe.set("taey:predict:face", result.get("face", ""), ex=TTL)
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
