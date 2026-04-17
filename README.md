# docker-net-attach

Lightweight, event-driven network controller for Docker that implements **Zero Trust microsegmentation** on a single host.

## Problem

In a typical reverse-proxy setup, all application containers share a single network (e.g. `proxy_net`). If one container is compromised, the attacker gains lateral access to every other container on that network — including backends and databases.

## Solution

Each application lives in its **own isolated network**. `docker-net-attach` dynamically connects gateway containers (Traefik, Mihomo, etc.) **only** to the networks explicitly declared by each application — and only while that application is healthy.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   app_a     │     │   app_b     │     │   app_c     │
│  net: a_net │     │  net: b_net │     │  net: c_net │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────── traefik ─┘                   │
        (dynamically attached             (not attached:
         while app is healthy)             no labels set)
```

## Quick Start

```yaml
# docker-compose.yml
services:
  docker-net-attach:
    image: ghcr.io/your-user/docker-net-attach:latest  # or build: .
    container_name: docker-net-attach
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - ALLOWED_GATEWAYS=traefik
```

Then label your application containers:

```yaml
# app/docker-compose.yml
services:
  myapp:
    image: myapp:latest
    labels:
      - "docker-net-attach.traefik.enable=true"
      - "docker-net-attach.traefik.network=myapp_net"
    networks:
      - myapp_net

networks:
  myapp_net:
    name: myapp_net
```

When `myapp` starts (and passes its healthcheck, if one is defined), the controller will automatically connect `traefik` to `myapp_net`. When `myapp` stops or becomes unhealthy, the controller disconnects `traefik` from that network.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ALLOWED_GATEWAYS` | *(required)* | Comma-separated list of gateway container names |
| `IGNORED_NETWORKS` | *(empty)* | Networks that should never be detached from gateways |
| `DOCKER_SOCKET` | `unix:///var/run/docker.sock` | Docker Engine API endpoint (Unix socket or TCP) |
| `LABEL_PREFIX` | `docker-net-attach` | Prefix for all container labels |
| `DRY_RUN` | `false` | Log actions without making changes |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Labels Reference

All labels follow the pattern `<prefix>.<gateway>.<key>`:

| Label | Required | Description |
|---|---|---|
| `docker-net-attach.<gw>.enable` | Yes | Set to `true` to activate |
| `docker-net-attach.<gw>.network` | Yes | Target network(s), comma-separated |

**Per-gateway ignored networks** (set on the gateway container itself):

| Label | Description |
|---|---|
| `docker-net-attach.ignored_networks` | Additional networks to never detach, comma-separated |

### Multi-gateway example

```yaml
labels:
  - "docker-net-attach.traefik.enable=true"
  - "docker-net-attach.traefik.network=app_frontend"
  - "docker-net-attach.mihomo.enable=true"
  - "docker-net-attach.mihomo.network=app_proxy"
```

## Architecture

The controller runs two concurrent loops:

1. **State Reconciler** — Computes the full desired-vs-actual network topology and reconciles the difference. Triggered on startup, gateway restarts, container shutdowns, and event stream reconnections. Debounced to collapse bursts into a single pass.

2. **Event Watcher** — Subscribes to Docker's `/events` stream and reacts in real-time:
   - `start` / `health_status: healthy` → **targeted O(1) attach** for the specific container
   - `die` / `health_status: unhealthy` → **full reconciliation** (other containers may still need the network)
   - Gateway `start` → **full reconciliation** (self-healing after gateway restart)

### Edge Cases Handled

- **Network validation** — The gateway is connected only to networks that the requesting container is actually a member of
- **Gateway self-healing** — When the gateway restarts, all dynamically attached networks are lost; the controller detects this and restores the topology
- **State desync cleanup** — Orphaned network attachments (from containers removed while the controller was offline) are detected and force-disconnected
- **Idempotent operations** — `409 Conflict` (already connected) and `404 Not Found` (already disconnected) are handled gracefully, not as errors
- **Automatic reconnection** — If the Docker event stream drops, the controller reconnects and runs a full sync to catch any missed events

## Tech Stack

- **Python 3.14+** with `asyncio`
- **httpx** — async HTTP client with Unix socket support
- **loguru** — structured logging

## License

[MIT](LICENSE)
