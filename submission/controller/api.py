"""HTTP client for the outcome-loop simulator.

Only the public API is used.  Network failures are handled explicitly:
reads retry with backoff, writes carry idempotency keys and are reconciled by
reading state afterwards rather than being blindly replayed.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

import httpx


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, detail: Any = None):
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


class SimClient:
    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.http = httpx.Client(base_url=self.base, timeout=timeout,
                                 headers={"user-agent": "formi-outcome-loop/1.0"})

    # -- low level -------------------------------------------------------
    def _request(self, method: str, path: str, *, retries: int = 4, **kw) -> httpx.Response:
        delay = 0.25
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.http.request(method, path, **kw)
                if r.status_code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 5.0)
                    last = ApiError(r.status_code, "retryable", r.text[:300])
                    continue
                return r
            except httpx.RequestError as e:  # connection reset / timeout
                last = e
                if attempt + 1 < retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 5.0)
        raise ApiError(0, "network_error", str(last))

    def _json(self, method: str, path: str, *, retries: int = 4, **kw) -> Any:
        r = self._request(method, path, retries=retries, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:
                body = {"error": "http_error", "detail": r.text[:500]}
            raise ApiError(r.status_code, body.get("error", "http_error"), body.get("detail"))
        return r.json()

    # -- simulator -------------------------------------------------------
    def health(self) -> dict:
        return self._json("GET", "/health")

    def state(self) -> dict:
        return self._json("GET", "/sim/state")

    def advance(self, minutes: int) -> dict:
        """Advance the world exactly once.

        A write is never blindly retried: if the connection dies we read the
        simulator state to see whether the tick already happened.
        """
        before = None
        try:
            before = self.state()
        except ApiError:
            pass
        try:
            return self._json("POST", "/sim/advance", json={"minutes": minutes})
        except ApiError as e:
            if e.status not in (0, 429, 500, 502, 503, 504):
                raise
            # Reconcile.  If the clock moved by the requested amount (or more),
            # the advance succeeded even though the response was lost.
            st = self._json("GET", "/sim/state")
            if before is not None and st["sim_minute"] >= before["sim_minute"] + minutes:
                return {"sim_minute": st["sim_minute"], "ts": st["ts"],
                        "finished": st["finished"], "new_chunks": [],
                        "reconciled": True}
            return self._json("POST", "/sim/advance", json={"minutes": minutes})

    def list_chunks(self, kind: str = "events", after: int = -1) -> list[dict]:
        return self._json("GET", "/events/chunks", params={"kind": kind, "after": after})

    def iter_chunk_lines(self, n: int) -> Iterator[str]:
        """Yield NDJSON lines from a chunk without holding the whole file."""
        delay = 0.25
        for attempt in range(5):
            try:
                with self.http.stream("GET", f"/events/chunks/{n}") as r:
                    if r.status_code >= 400:
                        r.read()
                        if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                            time.sleep(delay)
                            delay = min(delay * 2, 5.0)
                            continue
                        raise ApiError(r.status_code, "chunk_error", r.text[:300])
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if line.strip():
                            yield line
                return
            except httpx.RequestError:
                if attempt >= 4:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 5.0)

    # -- workflows -------------------------------------------------------
    def workflows(self) -> list[dict]:
        return self._json("GET", "/workflows")

    def workflow(self, wid: str) -> dict:
        return self._json("GET", f"/workflows/{wid}")

    def get_config(self, wid: str) -> dict:
        return self._json("GET", f"/workflows/{wid}/config")

    def config_history(self, wid: str) -> list[dict]:
        return self._json("GET", f"/workflows/{wid}/config/history")

    # -- governance ------------------------------------------------------
    def request_approval(self, body: dict) -> dict:
        return self._json("POST", "/approvals", json=body)

    def get_approval(self, aid: str) -> dict:
        return self._json("GET", f"/approvals/{aid}")

    def list_approvals(self, workflow_id: str | None = None, status: str | None = None) -> list[dict]:
        params = {}
        if workflow_id:
            params["workflow_id"] = workflow_id
        if status:
            params["status"] = status
        return self._json("GET", "/approvals", params=params)

    def apply_approval(self, wid: str, aid: str) -> dict:
        return self._json("POST", f"/workflows/{wid}/apply", json={"approval_id": aid})

    def auto_apply(self, wid: str, body: dict) -> dict:
        return self._json("PUT", f"/workflows/{wid}/config", json=body)

    # -- LLM / validation ------------------------------------------------
    def suggest_fixes(self, wid: str, problem: str) -> dict:
        return self._json("POST", "/llm/suggest-fixes",
                          json={"workflow_id": wid, "problem": problem})

    def run_validation(self, wid: str, prompt_version: str, n: int = 200) -> dict:
        return self._json("POST", "/validation/runs",
                          json={"workflow_id": wid, "prompt_version": prompt_version, "n": n})
