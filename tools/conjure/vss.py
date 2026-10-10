"""VAST VSS client: login, search, stream, YOLO sidecar. Tokens never leave the server."""

import os
import threading
import time

import requests

_lock = threading.Lock()
_token = {"value": None, "at": 0}


def _base():
    return os.environ["VSS_URL"].rstrip("/")


def configured():
    return all(os.environ.get(k) for k in ("VSS_URL", "VSS_USERNAME", "VSS_PASSWORD"))


def login(force=False):
    with _lock:
        if not force and _token["value"] and time.time() - _token["at"] < 20 * 60:
            return _token["value"]
        response = requests.post(
            f"{_base()}/api/v1/auth/login",
            json={"username": os.environ["VSS_USERNAME"], "password": os.environ["VSS_PASSWORD"]},
            timeout=15,
        )
        response.raise_for_status()
        _token["value"] = response.json()["access_token"]
        _token["at"] = time.time()
        return _token["value"]


def _headers():
    return {"Authorization": f"Bearer {login()}"}


def _request(method, path, **kwargs):
    url = f"{_base()}{path}"
    response = None
    for attempt in range(3):
        try:
            response = requests.request(method, url, headers=_headers(), **kwargs)
            if response.status_code == 401:
                login(force=True)
                response = requests.request(method, url, headers=_headers(), **kwargs)
            if response.status_code < 500:
                return response
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
        time.sleep(3 * (attempt + 1))
    return response


def search(query, top_k=24, camera_id=None):
    body = {"query": query, "top_k": top_k, "include_public": True}
    if camera_id:
        body["metadata_filters"] = {"camera_id": camera_id}
    response = _request("POST", "/api/v1/search", json=body, timeout=60)
    response.raise_for_status()
    return response.json().get("results") or []


def detections(source):
    response = _request("GET", "/api/v1/videos/detections", params={"source": source}, timeout=60)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def stream_url(source):
    """Server-side only: contains the JWT. Never send this to the browser or a log."""
    from urllib.parse import urlencode

    return f"{_base()}/api/v1/videos/stream?{urlencode({'source': source, 'token': login()})}"


def download(source, out_path):
    response = requests.get(
        f"{_base()}/api/v1/videos/stream", params={"source": source, "token": login()}, stream=True, timeout=120
    )
    if response.status_code == 401:
        login(force=True)
        response = requests.get(
            f"{_base()}/api/v1/videos/stream", params={"source": source, "token": login()}, stream=True, timeout=120
        )
    response.raise_for_status()
    tmp = out_path + ".part"
    with open(tmp, "wb") as handle:
        for chunk in response.iter_content(chunk_size=256 * 1024):
            handle.write(chunk)
    os.replace(tmp, out_path)
