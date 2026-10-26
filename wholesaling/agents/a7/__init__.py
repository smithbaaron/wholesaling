"""A7 offer engine (REQUIREMENTS.md section 11; build order BO-11).

Modules: ``comps`` (step 2), ``arv`` (step 3), ``repairs`` (step 4), ``mao`` (steps 5-6),
``gates`` (step 7), ``templates`` (step 8), ``audit`` (V3 model), ``verification`` (V1..V6
rules), ``recompute`` (V5, independent of ``mao``), ``engine`` (the pipeline).
"""

from wholesaling.agents.a7.engine import (
    STATUS_BLOCKED,
    STATUS_ESCALATE,
    STATUS_READY,
    A7OfferEngine,
    OfferComputation,
    PrefilterAuditor,
    SubjectInput,
)

__all__ = [
    "STATUS_BLOCKED",
    "STATUS_ESCALATE",
    "STATUS_READY",
    "A7OfferEngine",
    "OfferComputation",
    "PrefilterAuditor",
    "SubjectInput",
]
