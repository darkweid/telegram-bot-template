from collections import deque
import json
from typing import Any

import httpx2


class ScriptedLLMServer:
    """Answers the LLM SDKs' HTTP requests from a script and records them.

    Both SDKs run on httpx2, which respx does not patch; this server is the
    client's transport instead, so no request can leave the process. An
    unscripted request fails the call instead of reaching the network.
    """

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self._script: deque[httpx2.Response | type[httpx2.TransportError]] = deque()

    def reply(
        self,
        json_body: dict[str, Any],
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._script.append(httpx2.Response(status, json=json_body, headers=headers))

    def fail(self, error: type[httpx2.TransportError]) -> None:
        self._script.append(error)

    def http_client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self._script:
            raise AssertionError(f"unscripted request to {request.url}")
        step = self._script.popleft()
        if isinstance(step, httpx2.Response):
            return step
        raise step("scripted transport failure", request=request)
