import asyncio
import json
import urllib.parse
from collections.abc import AsyncGenerator

import httpx
from loguru import logger

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)


class DockerClient:
    """Async HTTP client for the Docker Engine API.

    Supports Unix socket and TCP connections. Use as an async context manager::

        async with DockerClient("unix:///var/run/docker.sock") as docker:
            containers = await docker.get_containers()
    """

    def __init__(self, socket_url: str) -> None:
        if socket_url.startswith("unix://"):
            transport = httpx.AsyncHTTPTransport(uds=socket_url[7:])
            base_url = "http://localhost/v1.40"
        elif socket_url.startswith(("http://", "https://")):
            transport = httpx.AsyncHTTPTransport()
            base_url = f"{socket_url.rstrip('/')}/v1.40"
        else:
            raise ValueError(
                f"Invalid DOCKER_SOCKET: '{socket_url}'. "
                "Expected: unix:///var/run/docker.sock or http://docker-socket-proxy:2375"
            )

        self._client = httpx.AsyncClient(transport=transport, base_url=base_url, timeout=_TIMEOUT)

    async def __aenter__(self) -> "DockerClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Generic request helper -------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response | None:
        """Centralised HTTP request with unified error handling."""
        try:
            response = await self._client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            logger.error(f"HTTP {exc.response.status_code} at {exc.request.url}: {exc.response.text.strip()[:300]}")
            raise
        except httpx.RequestError as exc:
            logger.error(f"Request error at {exc.request.url}: {exc}")
            raise

    # --- Read operations --------------------------------------------------------

    async def get_containers(self) -> list[dict] | None:
        """List all containers (running and stopped)."""
        resp = await self._request("GET", "/containers/json", params={"all": True})
        return resp.json() if resp else None

    async def get_container(self, container_id: str) -> dict | None:
        """Inspect a single container by ID or name."""
        resp = await self._request("GET", f"/containers/{container_id}/json")
        return resp.json() if resp else None

    # --- Network mutations (status-aware error handling) ------------------------

    async def connect_network(self, network: str, container_name: str) -> bool:
        """Attach a container to a network.

        Returns True on success or if already connected (409 is idempotent).
        """
        try:
            safe_net = urllib.parse.quote(network, safe="")
            resp = await self._client.request(
                "POST",
                f"/networks/{safe_net}/connect",
                json={"Container": container_name},
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 409:
                logger.debug(f"'{container_name}' already connected to '{network}' — skipped")
                return True
            if code == 404:
                logger.warning(f"Network '{network}' or container '{container_name}' not found")
                return False
            elif code == 403:
                logger.error(f"Permission denied connecting '{container_name}' to '{network}'")
                return False
            else:
                logger.error(
                    f"HTTP {code} connecting '{container_name}' to '{network}': {exc.response.text.strip()[:200]}"
                )
                raise
        except httpx.RequestError as exc:
            logger.error(f"Request error connecting '{container_name}' to '{network}': {exc}")
            raise

    async def disconnect_network(self, network: str, container_name: str) -> bool:
        """Detach a container from a network.

        Returns True on success or if already disconnected (404 is idempotent).
        """
        try:
            safe_net = urllib.parse.quote(network, safe="")
            resp = await self._client.request(
                "POST",
                f"/networks/{safe_net}/disconnect",
                json={"Container": container_name, "Force": True},
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 404:
                logger.debug(f"Network '{network}' or container '{container_name}' already gone — skipped")
                return True
            if code == 403:
                logger.error(f"Permission denied disconnecting '{container_name}' from '{network}'")
                return False
            else:
                logger.error(
                    f"HTTP {code} disconnecting '{container_name}' from '{network}': {exc.response.text.strip()[:200]}"
                )
                raise
        except httpx.RequestError as exc:
            logger.error(f"Request error disconnecting '{container_name}' from '{network}': {exc}")
            raise

    # --- Event stream -----------------------------------------------------------

    async def get_events(self, on_ready: asyncio.Event | None = None) -> AsyncGenerator[str, None]:
        """Subscribe to filtered Docker event stream. Yields raw JSON lines."""
        filters = {
            "type": ["container", "network"],
            "event": [
                "start",
                "die",
                "health_status: healthy",
                "health_status: unhealthy",
                "disconnect",
            ],
        }
        params = {"filters": json.dumps(filters)}

        try:
            async with self._client.stream("GET", "/events", params=params, timeout=None) as response:
                response.raise_for_status()
                if on_ready:
                    on_ready.set()
                async for line in response.aiter_lines():
                    if line:
                        yield line
        except httpx.HTTPStatusError as exc:
            logger.error(f"HTTP {exc.response.status_code} in event stream: {exc.response.text.strip()[:300]}")
            raise
        except httpx.RequestError as exc:
            logger.error(f"Request error in event stream: {exc}")
            raise
        finally:
            logger.debug("Event stream closed")
