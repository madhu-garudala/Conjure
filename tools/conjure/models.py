"""Hosted model calls: YOLO11 boxes, Cosmos3-Reason review/caption, W&B Inference chat."""

import base64
import json
import os
import re

import requests

import tracing

_cosmos = {"id": None}
WANDB_URL = "https://api.inference.wandb.ai/v1/chat/completions"
NL_MODELS = ("meta-llama/Llama-3.3-70B-Instruct", "meta-llama/Llama-3.1-8B-Instruct")


def _gpu_headers():
    return {"Authorization": f"Bearer {os.environ['GPU_BEARER_TOKEN']}"}


def gpu_configured():
    return bool(os.environ.get("GPU_BEARER_TOKEN") and os.environ.get("YOLO_URL"))


def wandb_configured():
    return all(os.environ.get(k) for k in ("WANDB_API_KEY", "WANDB_TEAM", "WANDB_PROJECT"))


def extract_json(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _b64(path):
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode()


def _yolo(filename):
    response = requests.post(
        f"{os.environ['YOLO_URL']}/v1/infer",
        headers=_gpu_headers(),
        json={"video_base64": _b64(filename), "filename": os.path.basename(filename), "include_frames": True},
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    frames = data.get("frames") or (data.get("perception_json") or {}).get("frames") or []
    return {"frames": frames, "object_counts": data.get("object_counts") or {}}


def yolo_frames(path):
    return tracing.call("yolo11.detect", _yolo, path, label=os.path.basename(path))


def _cosmos_model():
    if not _cosmos["id"]:
        response = requests.get(f"{os.environ['COSMOS3_REASON_URL']}/v1/models", headers=_gpu_headers(), timeout=10)
        response.raise_for_status()
        _cosmos["id"] = response.json()["data"][0]["id"]
    return _cosmos["id"]


def cosmos_model_id():
    try:
        return _cosmos_model()
    except Exception:
        return None


def _cosmos_video(path, prompt, max_tokens=260):
    response = requests.post(
        f"{os.environ['COSMOS3_REASON_URL']}/v1/chat/completions",
        headers=_gpu_headers(),
        json={
            "model": _cosmos_model(),
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{_b64(path)}"}},
            ]}],
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def cosmos_review(path, description):
    prompt = (
        "Watch this street-camera clip as a strict visual-effects reviewer. Judge only image quality: "
        "look for ghosting, translucent or partial bodies, smeared or blurred patches, flicker, "
        "objects that pop in or out, or pasted objects that look fake. Ignore deliberate privacy blur "
        "on faces and plates and the small 'Edited with Conjure' text. Return JSON only with keys: "
        '"natural" (bool), "score" (0-1, 1 = edit is invisible), "issues" (list of short strings), '
        '"summary" (one sentence).'
    )
    try:
        text = tracing.call("cosmos3.review_edit", _cosmos_video, path, prompt, label=description[:80])
    except Exception as exc:
        return {"ok": False, "summary": f"Cosmos review unavailable ({type(exc).__name__})."}
    parsed = extract_json(text)
    if not parsed:
        return {"ok": False, "summary": "Cosmos reply could not be read."}
    return {
        "ok": True,
        "natural": bool(parsed.get("natural")),
        "score": _clamp(parsed.get("score")),
        "issues": [str(i) for i in (parsed.get("issues") or [])][:4],
        "summary": str(parsed.get("summary") or "")[:300],
        "model": _cosmos["id"],
    }


def cosmos_caption(path):
    prompt = "Describe this video clip in two sentences: the scene, and the main objects and people that move."
    try:
        return tracing.call("cosmos3.caption", _cosmos_video, path, prompt, 160, label=os.path.basename(path)).strip()
    except Exception:
        return ""


def _wandb_chat(prompt, model, max_tokens=300):
    response = requests.post(
        WANDB_URL,
        headers={
            "Authorization": f"Bearer {os.environ['WANDB_API_KEY']}",
            "OpenAI-Project": f"{os.environ['WANDB_TEAM']}/{os.environ['WANDB_PROJECT']}",
        },
        json={"model": model, "max_tokens": max_tokens, "temperature": 0,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def wandb_chat(prompt, label=None):
    """Returns (text, model_id). Raises if every model is unreachable."""
    last = None
    for model in NL_MODELS:
        try:
            return tracing.call("wandb.parse_command", _wandb_chat, prompt, model, label=label), model
        except Exception as exc:
            last = exc
    raise last


def _clamp(value):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None
