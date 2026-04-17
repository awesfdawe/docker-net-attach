import os
import re
from dataclasses import dataclass

_VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _parse_frozenset(raw: str) -> frozenset[str]:
    """Parse a comma-separated string into a frozenset, stripping whitespace."""
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Config:
    """Immutable application configuration.

    Use ``Config.from_env()`` to build from environment variables, or construct
    directly in tests with explicit values — no ENV setup required.
    """

    docker_socket: str
    label_prefix: str
    dry_run: bool
    log_level: str
    allowed_gateways: frozenset[str]
    ignored_networks: frozenset[str]

    @classmethod
    def from_env(cls) -> "Config":
        """Build configuration from environment variables."""
        raw_gateways = os.getenv("ALLOWED_GATEWAYS", "")
        if not raw_gateways.strip():
            raise ValueError("ALLOWED_GATEWAYS environment variable is required and cannot be empty.")

        gateways = _parse_frozenset(raw_gateways)
        for gw in gateways:
            if not _VALID_NAME.match(gw):
                raise ValueError(f"Invalid gateway name '{gw}'. Only [a-zA-Z0-9_.-] characters are allowed.")

        return cls(
            docker_socket=os.getenv("DOCKER_SOCKET", "unix:///var/run/docker.sock"),
            label_prefix=os.getenv("LABEL_PREFIX", "docker-net-attach"),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            allowed_gateways=gateways,
            ignored_networks=_parse_frozenset(os.getenv("IGNORED_NETWORKS", "")),
        )
