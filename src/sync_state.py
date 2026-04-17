import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from loguru import logger

from src.config import Config
from src.docker import DockerClient

# Type alias for connect/disconnect methods
NetworkAction = Callable[[str, str], Coroutine[Any, Any, bool]]


@dataclass(frozen=True)
class ContainerIntent:
    """Parsed label declaration for a single gateway attachment."""

    gateway: str
    networks: list[str]


# --- Helpers -----------------------------------------------------------------


def parse_intents(labels: dict[str, str], config: Config) -> list[ContainerIntent]:
    """Extract all gateway attachment intents from a label/attribute dict.

    Works with both ``/containers/json`` label maps and ``Actor.Attributes``
    from Docker events (labels are included as flat key-value pairs in both).
    """
    intents: list[ContainerIntent] = []
    for gw in config.allowed_gateways:
        if labels.get(f"{config.label_prefix}.{gw}.enable", "").lower() != "true":
            continue
        raw = labels.get(f"{config.label_prefix}.{gw}.network", "")
        networks = [n.strip() for n in raw.split(",") if n.strip()]
        intents.append(ContainerIntent(gateway=gw, networks=networks))
    return intents


def _container_names(container: dict) -> list[str]:
    """Extract clean names from a container list JSON object."""
    return [n.lstrip("/") for n in container.get("Names", [])]


def _is_ready(container: dict) -> bool:
    """Return True if the container is running and healthy (or has no healthcheck).

    Uses the human-readable ``Status`` field from ``/containers/json`` which
    includes health status in parentheses, e.g. ``"Up 2 hours (healthy)"``.
    """
    if container.get("State") != "running":
        return False
    status = container.get("Status", "")
    if "(unhealthy)" in status or "(health: starting)" in status:
        return False
    return True


def _is_ready_inspect(container: dict) -> bool:
    """Return True if an inspected container is running and healthy.

    Uses machine-readable ``State.Health.Status`` from ``/containers/{id}/json``
    which is more reliable than parsing human-readable Status strings.
    """
    state = container.get("State", {})
    if state.get("Status") != "running":
        return False
    health = state.get("Health")
    if health is None:
        return True  # No healthcheck defined — running is sufficient
    return health.get("Status") == "healthy"


def _build_ignored(gateway_container: dict, config: Config) -> set[str]:
    """Merge global IGNORED_NETWORKS with per-gateway ``ignored_networks`` label."""
    ignored = set(config.ignored_networks)
    label = gateway_container.get("Labels", {}).get(f"{config.label_prefix}.ignored_networks")
    if label:
        ignored.update(n.strip() for n in label.split(",") if n.strip())
    return ignored


async def _apply(
    action: str,
    method: NetworkAction,
    gateway: str,
    networks: set[str],
    *,
    dry_run: bool = False,
) -> None:
    """Connect or disconnect a set of networks for a gateway, with DRY_RUN support."""
    if not networks:
        return

    if dry_run:
        for net in sorted(networks):
            logger.info(
                f"[DRY RUN] Would {action.lower()} '{gateway}' "
                f"{'to' if action == 'ATTACH' else 'from'} '{net}'"
            )
        return

    results = await asyncio.gather(
        *(method(net, gateway) for net in networks),
        return_exceptions=True,
    )
    for net, result in zip(networks, results):
        if isinstance(result, Exception):
            logger.error(f"[{action}] Failed for '{gateway}' / '{net}': {result}")
        elif result:
            logger.success(f"[{action}] '{gateway}' ↔ '{net}'")
        else:
            logger.error(f"[{action}] Failed for '{gateway}' / '{net}'")


# --- Public API --------------------------------------------------------------


async def sync_state(docker: DockerClient, config: Config) -> None:
    """Compute desired vs actual state and reconcile gateway network attachments.

    Performs a full reconciliation: fetches all containers, computes the desired
    network topology from labels, then attaches/detaches networks as needed.
    """
    containers = await docker.get_containers()
    if containers is None:
        logger.error("Failed to fetch containers for state sync")
        return

    # Phase 1: identify gateway containers
    gateways: dict[str, dict] = {}
    for container in containers:
        for name in _container_names(container):
            if name in config.allowed_gateways:
                if name in gateways:
                    logger.warning(f"Duplicate gateway '{name}' detected — using latest")
                gateways[name] = container

    # Phase 2: build desired network map from labelled containers
    desired: dict[str, set[str]] = {gw: set() for gw in config.allowed_gateways}

    for container in containers:
        names = _container_names(container)
        if any(n in config.allowed_gateways for n in names):
            continue  # Skip gateway containers themselves

        labels = container.get("Labels", {}) or {}
        cid = container.get("Id", "")[:12]
        actual_nets = set((container.get("NetworkSettings", {}).get("Networks", {}) or {}).keys())
        ready = _is_ready(container)

        intents = parse_intents(labels, config)
        for intent in intents:
            if not intent.networks:
                logger.warning(
                    f"Container '{cid}' has {config.label_prefix}.{intent.gateway}.enable=true "
                    f"but no network label"
                )
                continue

            for net in intent.networks:
                if net not in actual_nets:
                    logger.warning(f"Container '{cid}' requests '{net}' but is not a member — rejected")
                    continue
                if ready:
                    desired[intent.gateway].add(net)

    # Phase 3: reconcile per gateway
    for gw, desired_nets in desired.items():
        gw_container = gateways.get(gw)
        if not gw_container:
            logger.debug(f"Gateway '{gw}' not found — skipping")
            continue

        current_nets = set((gw_container.get("NetworkSettings", {}).get("Networks", {}) or {}).keys())
        ignored = _build_ignored(gw_container, config)

        to_attach = desired_nets - current_nets
        to_detach = current_nets - desired_nets - ignored

        await _apply("ATTACH", docker.connect_network, gw, to_attach, dry_run=config.dry_run)
        await _apply("DETACH", docker.disconnect_network, gw, to_detach, dry_run=config.dry_run)


async def targeted_attach(docker: DockerClient, config: Config, container_id: str) -> bool:
    """Perform targeted network attachment for a single container.

    Used for O(1) event handling on ``start`` / ``healthy`` events instead of
    running a full ``sync_state``.  Returns True if any action was taken.

    Uses ``/containers/{id}/json`` (inspect) for reliable machine-readable
    health status rather than parsing human-readable Status strings.
    """
    container = await docker.get_container(container_id)
    if container is None:
        return False

    # Don't process gateway containers themselves
    name = (container.get("Name") or "").lstrip("/")
    if name in config.allowed_gateways:
        return False

    # Verify the container is still running and healthy (may have died between event and inspect)
    if not _is_ready_inspect(container):
        logger.debug(f"Container '{name}' is no longer ready — skipping attach")
        return False

    labels = container.get("Config", {}).get("Labels", {}) or {}
    actual_nets = set((container.get("NetworkSettings", {}).get("Networks", {}) or {}).keys())

    intents = parse_intents(labels, config)
    if not intents:
        return False

    acted = False
    for intent in intents:
        for net in intent.networks:
            if net not in actual_nets:
                logger.warning(f"Container '{name}' requests '{net}' but is not a member — skipped")
                continue
            if config.dry_run:
                logger.info(f"[DRY RUN] Would attach '{intent.gateway}' to '{net}'")
                acted = True
            else:
                ok = await docker.connect_network(net, intent.gateway)
                if ok:
                    logger.success(f"[ATTACH] '{intent.gateway}' ↔ '{net}'")
                    acted = True
    return acted
