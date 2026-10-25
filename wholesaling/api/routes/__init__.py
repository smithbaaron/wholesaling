"""API route modules. Each module exposes a ``router``; ``create_app`` mounts them."""

from wholesaling.api.routes import health

__all__ = ["health"]
