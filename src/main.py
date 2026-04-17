import asyncio
import json
import signal
import sys

from loguru import logger

from src.config import Config
from src.docker import DockerClient
from src.sync_state import parse_intents, sync_state, targeted_attach

_DEBOUNCE_SECONDS = 1.5
_RECONNECT_BASE_DELAY = 5
_RECONNECT_MAX_DELAY = 120


class Controller:
    """Event-driven controller that reconciles gateway network attachments.

    Routes events to either a targeted O(1) attach (for ``start`` / ``healthy``)
    or a debounced full reconciliation (for ``die`` / ``unhealthy`` / gateway restart).
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._docker = DockerClient(config.docker_socket)
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=0)
        self._shutdown = asyncio.Event()
        self._sync_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()

    # --- Internal workers --------------------------------------------------------

    async def _reconciler(self) -> None:
        """Serialise and debounce ``sync_state`` calls.

        Drains all accumulated events during the debounce window to collapse
        bursts (e.g. ``docker compose up`` starting 10 containers) into a
        single reconciliation pass.
        """
        while not self._shutdown.is_set():
            try:
                trigger = await self._queue.get()
                self._queue.task_done()
                await asyncio.sleep(_DEBOUNCE_SECONDS)

                # Drain events accumulated during debounce window (limit by current size)
                drained = 0
                max_drain = self._queue.qsize()
                while drained < max_drain:
                    try:
                        self._queue.get_nowait()
                        self._queue.task_done()
                        drained += 1
                    except asyncio.QueueEmpty:
                        break

                logger.debug(f"Reconciliation triggered by '{trigger}' (drained {drained} extra events)")
                async with self._sync_lock:
                    await sync_state(self._docker, self._config)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Error in reconciler: {exc}")

    async def _watcher(self, on_ready: asyncio.Event | None = None) -> None:
        """Watch Docker event stream with automatic reconnection and exponential backoff."""
        attempt = 0
        while not self._shutdown.is_set():
            try:
                logger.info("Event watcher connecting...")
                async for line in self._docker.get_events(on_ready=on_ready):
                    attempt = 0  # Reset backoff on successful message
                    if self._shutdown.is_set():
                        break
                    try:
                        await self._handle_event(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                    except Exception as exc:
                        logger.error(f"Error processing event: {exc}")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._shutdown.is_set():
                    return
                attempt += 1
                delay = min(_RECONNECT_BASE_DELAY * (2 ** (attempt - 1)), _RECONNECT_MAX_DELAY)
                logger.warning(f"Event stream lost: {exc}. Reconnecting in {delay}s (attempt {attempt})...")
                await asyncio.sleep(delay)
                await self._queue.put("reconnect_sync")

    # --- Event handling ----------------------------------------------------------

    async def _handle_event(self, event: dict) -> None:
        """Route a single Docker event to the appropriate action.

        * ``start`` / ``healthy`` on labelled containers → targeted O(1) attach
        * ``die`` / ``unhealthy`` on labelled containers → full sync (other containers may still need the networks)
        * ``start`` of a gateway container → full sync (self-healing after gateway restart)
        * ``disconnect`` of a gateway from a network → full sync (self-healing after manual detach)
        """
        status = event.get("status")
        event_type = event.get("Type") or event.get("type", "container")
        actor = event.get("Actor", {})
        attrs = actor.get("Attributes", {})

        # Self-Healing: gateway disconnected from a network (e.g. manual CLI detach).
        # Only trigger when the disconnected container is a known gateway to avoid
        # cascading reconciliations from our own detach operations.
        if event_type == "network" and status == "disconnect":
            disconnected_id = attrs.get("container", "")
            if disconnected_id:
                try:
                    info = await self._docker.get_container(disconnected_id)
                except Exception:
                    return
                if info:
                    cname = (info.get("Name") or "").lstrip("/")
                    if cname in self._config.allowed_gateways:
                        logger.info(f"Gateway '{cname}' disconnected from network — triggering reconciliation")
                        await self._queue.put("network_disconnect")
            return

        container_id = actor.get("ID", "")
        name = attrs.get("name", "")
        cid = container_id[:12]

        # Self-Healing: gateway restart → full reconciliation
        if name in self._config.allowed_gateways and status == "start":
            logger.info(f"Gateway '{name}' started — triggering full reconciliation")
            await self._queue.put("gateway_restart")
            return

        # Only react to containers carrying our labels
        intents = parse_intents(attrs, self._config)
        if not intents:
            return

        if status in ("start", "health_status: healthy"):
            # Targeted attach — O(1) operation, no full sync needed
            logger.info(f"Container '{name}' ({cid}) → {status} — targeted attach")

            async def _bg_attach() -> None:
                try:
                    async with self._sync_lock:
                        await targeted_attach(self._docker, self._config, container_id)
                except Exception as exc:
                    logger.error(f"Targeted attach failed for '{name}': {exc} — falling back to full sync")
                    await self._queue.put(f"fallback_{status}")

            task = asyncio.create_task(_bg_attach())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

        elif status in ("die", "health_status: unhealthy"):
            logger.info(f"Container '{name}' ({cid}) → {status} — scheduling full sync for detach")
            # We explicitly skip targeted detach here because removing a network
            # might break other containers still relying on it. Let sync_state handle it.
            await self._queue.put(f"event_{status}")

    # --- Lifecycle ---------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point: initial sync, then start workers and wait for shutdown."""
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, self._signal_shutdown)
            loop.add_signal_handler(signal.SIGTERM, self._signal_shutdown)
        except NotImplementedError:
            pass  # Windows

        logger.info("Starting docker-net-attach...")
        if self._config.dry_run:
            logger.warning("DRY_RUN mode — no real changes will be made")

        watcher_ready = asyncio.Event()

        reconciler = asyncio.create_task(self._reconciler())
        watcher = asyncio.create_task(self._watcher(on_ready=watcher_ready))

        # Reconciler and stream must be running BEFORE sync_state to avoid race condition
        # Wait for the watcher to establish the HTTP stream connection
        try:
            await asyncio.wait_for(watcher_ready.wait(), timeout=10.0)
            logger.info("Event stream connected, stream buffering...")
        except asyncio.TimeoutError:
            logger.warning("Event stream took too long to connect, proceeding with sync anyway")

        # Run initial reconciliation before reacting to any buffered events.
        try:
            async with self._sync_lock:
                await sync_state(self._docker, self._config)
            logger.info("Initial reconciliation complete")
        except Exception as exc:
            logger.error(f"Initial reconciliation failed: {exc}")

        await self._shutdown.wait()

        logger.info("Shutting down...")
        reconciler.cancel()
        watcher.cancel()
        for task in self._bg_tasks:
            task.cancel()
        await asyncio.gather(reconciler, watcher, *self._bg_tasks, return_exceptions=True)
        await self._docker.aclose()
        logger.info("Shutdown complete")

    def _signal_shutdown(self) -> None:
        self._shutdown.set()


def main() -> None:
    try:
        config = Config.from_env()
        logger.remove()
        logger.add(sys.stderr, level=config.log_level)
        asyncio.run(Controller(config).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
