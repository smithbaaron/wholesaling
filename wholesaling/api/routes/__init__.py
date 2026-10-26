"""API route modules. Each module exposes a ``router``; ``create_app`` mounts them."""

from wholesaling.api.routes import deals, digest, graph_webhook, health, pilot, policies, review

__all__ = ["deals", "digest", "graph_webhook", "health", "pilot", "policies", "review"]
