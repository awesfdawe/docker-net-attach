import asyncio
import json
import signal
import sys

from loguru import logger

from src.config import Config
from src.docker import DockerClient
from src.sync_state import parse_intents, sync_state, targeted_attach

_DEBOUNCE_SECONDS = 0.5
_RECONNECT_DELAY = 5


class Controller:
    """Event-driven controller that reconciles gateway network attachments.

    Routes events to either a targeted O(1) attach (for ``start`` / ``healthy``)
    or a debounced full reconciliation (for ``die`` / ``unhealthy`` / gateway restart).
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._docker = DockerClient(config.docker_socket)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._shutdown = asyncio.Event()

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
                await asyncio.sleep(_DEBOUNCE_SECONDS)

                # Drain all events accumulated during debounce window
                drained = 0
                while True:
                    try:
                        self._queue.get_nowait()
                        drained += 1
                    except asyncio.QueueEmpty:
                        break

                logger.debug(f"Reconciliation triggered by '{trigger}' (drained {drained} extra events)")
                await sync_state(self._docker, self._config)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Error in reconciler: {exc}")

    async def _watcher(self) -> None:
        """Watch Docker event stream with automatic reconnection."""
        while not self._shutdown.is_set():
            try:
                logger.info("Event watcher connected")
                async for line in self._docker.get_events():
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
                logger.warning(
                    f"Event stream lost: {exc}. "
                    f"Events during the next ~{_RECONNECT_DELAY}s may be missed. "
                    f"Reconnecting with full sync..."
                )
                await asyncio.sleep(_RECONNECT_DELAY)
                await self._queue.put("reconnect_sync")

    # --- Event handling ----------------------------------------------------------

    async def _handle_event(self, event: dict) -> None:
        """Route a single Docker event to the appropriate action.

        * ``start`` / ``healthy`` on labelled containers → targeted O(1) attach
        * ``die`` / ``unhealthy`` on labelled containers → full sync (other containers may still need the networks)
        * ``start`` of a gateway container → full sync (self-healing after gateway restart)
        """
        status = event.get("status")
        actor = event.get("Actor", {})
        attrs = actor.get("Attributes", {})
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
            try:
                await targeted_attach(self._docker, self._config, container_id)
            except Exception as exc:
                logger.error(f"Targeted attach failed for '{name}': {exc} — falling back to full sync")
                await self._queue.put(f"fallback_{status}")

        elif status in ("die", "health_status: unhealthy"):
            # Full sync required — other containers may still need the networks
            logger.info(f"Container '{name}' ({cid}) → {status} — queuing full reconciliation")
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

        # Run initial reconciliation before starting the event watcher so that
        # the system is in a consistent state before we begin reacting to events.
        try:
            await sync_state(self._docker, self._config)
            logger.info("Initial reconciliation complete")
        except Exception as exc:
            logger.error(f"Initial reconciliation failed: {exc}")

        reconciler = asyncio.create_task(self._reconciler())
        watcher = asyncio.create_task(self._watcher())

        await self._shutdown.wait()

        logger.info("Shutting down...")
        reconciler.cancel()
        watcher.cancel()
        await asyncio.gather(reconciler, watcher, return_exceptions=True)
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
