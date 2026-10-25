"""Wholesaling automation system.

Package layout (see docs/adr/0001-repo-layout.md):

- ``config``       settings from env (pydantic-settings) and versioned YAML tunables
- ``db``           SQLAlchemy models, Alembic migrations, repositories
- ``api``          FastAPI app: health, webhooks, web form, review queue
- ``workflows``    Temporal workflows (durable sequences)
- ``activities``   Temporal activities (agents, integrations, DB writes)
- ``workers``      Temporal worker entrypoint
- ``integrations`` one subpackage per external service, each with interface + mock + real
- ``agents``       agent definitions and the provider-agnostic model layer (``agents/llm.py``)
- ``policy``       autonomy policy engine (send vs queue_for_human)
"""

__version__ = "0.1.0"
