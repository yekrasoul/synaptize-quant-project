"""Small production-safe transport for unauthenticated public JSON GETs."""

from __future__ import annotations

import http.client
import socket
import time
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlencode, urlsplit


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class TransportError(Exception):
    """Base class for failures before a usable HTTP response is available."""

    def __init__(self, message: str, *, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(message)


class TransportTimeout(TransportError):
    """All bounded attempts timed out."""


class TransportConnectionError(TransportError):
    """All bounded attempts failed to connect or exchange bytes."""


class PublicHttpTransport:
    """HTTPS-only transport with explicit timeouts and bounded retry."""

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("public market-data base_url must be HTTPS")
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("HTTP timeouts must be positive")
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be within 1..5")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        self._host = parsed.hostname
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep

    def get(self, path: str, params: Mapping[str, str | int]) -> HttpResponse:
        if not path.startswith("/"):
            raise ValueError("HTTP path must be absolute")
        query = urlencode(params)
        target = f"{self._base_path}{path}"
        if query:
            target = f"{target}?{query}"

        last_error: OSError | TimeoutError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._request_once(target)
            except (socket.timeout, TimeoutError) as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    raise TransportTimeout(
                        "public HTTP request timed out after bounded retry",
                        attempts=attempt,
                    ) from exc
            except OSError as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    raise TransportConnectionError(
                        "public HTTP request failed after bounded retry",
                        attempts=attempt,
                    ) from exc
            else:
                if response.status not in {429} and not 500 <= response.status <= 599:
                    return response
                if attempt == self.max_attempts:
                    return response

            self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))

        raise AssertionError(f"unreachable transport state: {last_error!r}")

    def _request_once(self, target: str) -> HttpResponse:
        connection = http.client.HTTPSConnection(
            self._host,
            self._port,
            timeout=self.connect_timeout_seconds,
        )
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "btc-dca-bridge/phase3.3",
                },
            )
            if connection.sock is not None:
                connection.sock.settimeout(self.read_timeout_seconds)
            raw = connection.getresponse()
            return HttpResponse(
                status=raw.status,
                headers={key.lower(): value for key, value in raw.getheaders()},
                body=raw.read(),
            )
        finally:
            connection.close()
