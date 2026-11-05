"""HTTP client for the server's worker API. Every call has a timeout."""

import httpx
import pydantic

from pn_shared.api import (ClaimRequest, ClaimResponse, FailRequest, FailResponse, HeartbeatRequest, HeartbeatResponse,
                           HelloRequest, HelloResponse, PingResponse, ScanResult, ScanResultResponse)

from . import __version__

TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)


class AuthError(Exception):
    """The server rejected the token (401 revoked/unknown, 403 deactivated)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"server rejected the worker token ({status}: {detail})")
        self.status = status


class TaskLost(Exception):
    """The server says this task is not (or no longer) ours: the lease expired and it was handed on."""


class ServerError(Exception):
    """Server unreachable or answering with an unexpected status."""


class ApiClient:
    def __init__(self, server_url: str, token: str, transport: httpx.BaseTransport | None = None):
        # trust_env=False: proxy variables meant for yt-dlp (a VPN proxy) must not redirect the calls to the server.
        self._http = httpx.Client(base_url=server_url + "/api/worker", timeout=TIMEOUT, transport=transport, trust_env=False,
                                  headers={"Authorization": f"Bearer {token}",
                                           "User-Agent": f"podcaster-ng-worker/{__version__}"})

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # connection refused, timeout, TLS, ...
            raise ServerError(f"{type(exc).__name__}: {exc}") from None
        if response.status_code in (401, 403):
            detail = ""
            try:
                detail = str(response.json().get("detail", ""))
            except ValueError:
                pass
            raise AuthError(response.status_code, detail)
        if response.status_code in (404, 409) and "/tasks/" in path:
            raise TaskLost(f"task not available to this worker ({response.status_code})")
        if response.status_code != 200:
            raise ServerError(f"unexpected status {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ServerError("the server answered with something that is not JSON") from None

    @staticmethod
    def _parse(model, data: dict):
        try:
            return model.model_validate(data)
        except pydantic.ValidationError:
            raise ServerError("the server answered with an unexpected format") from None

    def hello(self, request: HelloRequest) -> HelloResponse:
        return self._parse(HelloResponse, self._call("POST", "/hello", json=request.model_dump(exclude_none=True)))

    def ping(self) -> PingResponse:
        return self._parse(PingResponse, self._call("GET", "/ping"))

    def claim(self, request: ClaimRequest) -> ClaimResponse:
        return self._parse(ClaimResponse, self._call("POST", "/claim", json=request.model_dump()))

    def heartbeat(self, task_id: int, progress: str | None = None) -> HeartbeatResponse:
        body = HeartbeatRequest(progress=progress).model_dump(exclude_none=True)
        return self._parse(HeartbeatResponse, self._call("POST", f"/tasks/{task_id}/heartbeat", json=body))

    def scan_result(self, task_id: int, result: ScanResult) -> ScanResultResponse:
        return self._parse(ScanResultResponse, self._call("POST", f"/tasks/{task_id}/scan-result", json=result.model_dump()))

    def fail(self, task_id: int, request: FailRequest) -> FailResponse:
        return self._parse(FailResponse, self._call("POST", f"/tasks/{task_id}/fail", json=request.model_dump(exclude_none=True)))
