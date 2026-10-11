from __future__ import annotations

import httpx
from .config import settings


class LightRAGClient:
    def __init__(self) -> None:
        self.base_url = settings.lightrag_base_url.rstrip("/")
        self.headers = {"X-API-Key": settings.lightrag_internal_api_key}
        self.timeout = settings.request_timeout_seconds

    async def get(self, path: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}{path}", headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    async def post_json(self, path: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}{path}", headers=self.headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def post_file(self, path: str, file_path: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.split("/")[-1], f)}
                resp = await client.post(f"{self.base_url}{path}", headers=self.headers, files=files)
            resp.raise_for_status()
            return resp.json()

    async def post_scan(self) -> dict:
        """``POST /documents/scan`` with no body (LightRAG v1.5.7).

        Triggers LightRAG's own scan of its ``--input-dir``. The call is global:
        it covers every file in that directory and returns ONE
        ``{status, message, track_id}`` for the whole scan. When the pipeline is
        busy it answers ``status="scanning_skipped_pipeline_busy"`` with no
        ``track_id`` and starts nothing. A scan also performs LightRAG's
        "exclusive FAILED reset", pulling previously failed documents back in;
        that is expected and acceptable here.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/documents/scan", headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    async def proxy(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None):
        headers = dict(self.headers)
        if content_type:
            headers["Content-Type"] = content_type
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.request(method, f"{self.base_url}{path}", headers=headers, content=body)
            resp.raise_for_status()
            return resp


client = LightRAGClient()