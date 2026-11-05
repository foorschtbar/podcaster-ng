"""HTTP client for the server's worker API. Every call has a timeout."""

import httpx
import pydantic

from pn_shared.api import HelloRequest, HelloResponse, PingResponse

from . import __version__

TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)


class AuthError(Exception):
    """The server rejected the token (401 revoked/unknown, 403 deactivated)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"server rejected the worker token ({status}: {detail})")
        self.status = status


class ServerError(Exception):
    """Server unreachable or answering with an unexpected status."""


class ApiClient:
    def __init__(self, server_url: str, token: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(base_url=server_url + "/api/worker", timeout=TIMEOUT, transport=transport,
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
