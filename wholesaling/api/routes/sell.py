"""``GET|POST /sell``: the public seller web form with photo upload (REQUIREMENTS.md SEL-06,
SEL-08, SEL-12, INT-26, XC-03, CMP-03, CMP-05, ENG-Q9, ENG-Q13).

Unauthenticated (homeowners use it), server-rendered, rate-limited per client IP in process
(``SELL_FORM_RATE_LIMIT_PER_MINUTE``; Railway sets ``X-Forwarded-For``). No JavaScript needed.

``POST /sell``:

1. validate (address, name, email, consent to email contact; optional motivation / condition /
   issues / timeline / asking price; up to ``SELL_FORM_MAX_PHOTOS`` images of at most
   ``SELL_FORM_MAX_PHOTO_BYTES`` each);
2. the hidden ``submission_id`` (a UUID the GET issued; the server issues one when absent) is
   the idempotency key (SEL-12, XC-03): a re-POST finds the lead already on record and renders
   the thank-you page again - no second lead, intake, message, blob set or workflow;
3. photos go to ``BlobStorage`` under ``sell/<submission id>/<n>.<ext>``;
4. the lead is created (``source=web_form``) or merged into the lead already on record for the
   address (S1) or for the ``lead`` token S4 put in its upload link; the intake gets the photos;
   a ``messages`` row (channel ``web_form``, key ``form:<submission id>``) carries the fields;
5. one ``web_form.submitted`` audit event (same key), commit;
6. ``SellerInboundWorkflow`` is started with id ``seller-inbound:form:<submission id>`` (the
   mock gateway records it); the workflow runs S4 on the stored fields;
7. thank-you page.

Only the seller's own statements are stored (CMP-05): no IP address, no user agent.
"""

from __future__ import annotations

import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from starlette.datastructures import UploadFile

from wholesaling.activities.seller import (
    SELLER_INBOUND_WORKFLOW_NAME,
    SOURCE_WEB_FORM,
    seller_inbound_workflow_id,
)
from wholesaling.agents.templates.seller_outreach import disclosure
from wholesaling.api.deps import (
    SessionDep,
    SettingsDep,
    TemplatesDep,
    TemporalDep,
)
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import Actor, Channel, ConditionClass, IntakeTimeline
from wholesaling.db.models.markets import Market
from wholesaling.db.models.sellers import SellerLead
from wholesaling.db.normalize import normalize_address, normalize_email, normalize_phone
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.sellers import SellerRepository
from wholesaling.integrations.blob import BlobStorage, get_blob_storage
from wholesaling.logging import get_logger
from wholesaling.workflows.seller_inbound import SellerInboundInput

log = get_logger(__name__)
router = APIRouter(tags=["sell"])

EVENT_SUBMITTED = "web_form.submitted"
EVENT_START_FAILED = "web_form.workflow_start_failed"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EXT_RE = re.compile(r"^[a-z0-9]{1,5}$")
_CONTENT_TYPE_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "image/heif": "heif",
    "image/webp": "webp",
    "image/gif": "gif",
}

CONDITION_LABELS: dict[str, str] = {
    ConditionClass.LIGHT.value: "Light - move-in ready or cosmetic work only",
    ConditionClass.MEDIUM.value: "Medium - some rooms or systems need work",
    ConditionClass.HEAVY.value: "Heavy - major repairs (roof, foundation, kitchen/baths)",
    ConditionClass.GUT.value: "Gut - uninhabitable, needs everything",
}
TIMELINE_LABELS: dict[str, str] = {
    IntakeTimeline.ASAP.value: "As soon as possible",
    IntakeTimeline.WITHIN_30_DAYS.value: "Within 30 days",
    IntakeTimeline.WITHIN_90_DAYS.value: "Within 90 days",
    IntakeTimeline.FLEXIBLE.value: "Flexible",
}
TEXT_FIELDS = (
    "address_line1",
    "city",
    "state",
    "postal_code",
    "name",
    "email",
    "phone",
    "motivation",
    "condition_class",
    "disclosed_issues",
    "timeline",
    "asking_price",
)
MAX_TEXT = 2000


# --------------------------------------------------------------------------------------------
# In-process rate limit
# --------------------------------------------------------------------------------------------


class IpRateLimiter:
    """Sliding one-minute window per client IP; process-local (one API container)."""

    def __init__(self, limit_per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit_per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, ip: str) -> bool:
        now = self._clock()
        window = self._hits.setdefault(ip, deque())
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= self.limit:
            return False
        window.append(now)
        if len(self._hits) > 10_000:  # bounded memory under a scan
            for key in [k for k, v in self._hits.items() if not v or now - v[-1] >= 60.0]:
                self._hits.pop(key, None)
        return True


def get_rate_limiter(request: Request) -> IpRateLimiter:
    limiter = getattr(request.app.state, "sell_rate_limiter", None)
    if limiter is None:
        settings: Settings = request.app.state.settings
        limiter = IpRateLimiter(settings.sell_form_rate_limit_per_minute)
        request.app.state.sell_rate_limiter = limiter
    assert isinstance(limiter, IpRateLimiter)
    return limiter


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_blob_storage_dep(request: Request) -> BlobStorage:
    blob = getattr(request.app.state, "blob", None)
    if blob is None:
        blob = get_blob_storage(request.app.state.settings)
        request.app.state.blob = blob
    return blob


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class PhotoUpload:
    original_name: str
    content_type: str
    data: bytes


@dataclass(slots=True)
class FormSubmission:
    submission_id: str
    values: dict[str, str]
    lead_token: str | None
    consent_email: bool
    photos: list[PhotoUpload] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _ext_for(upload: PhotoUpload) -> str:
    suffix = PurePosixPath(upload.original_name or "").suffix.lstrip(".").lower()
    if _EXT_RE.match(suffix):
        return "jpg" if suffix == "jpeg" else suffix
    return _CONTENT_TYPE_EXT.get(upload.content_type.lower(), "bin")


def _uuid_or_new(value: str | None) -> str:
    try:
        return str(uuid.UUID(str(value))) if value else str(uuid.uuid4())
    except ValueError:
        return str(uuid.uuid4())


async def parse_submission(request: Request, settings: Settings) -> FormSubmission:
    form = await request.form()
    values: dict[str, str] = {}
    for key in TEXT_FIELDS:
        raw = form.get(key)
        values[key] = (raw if isinstance(raw, str) else "").strip()[:MAX_TEXT]
    lead_raw = form.get("lead")
    lead_token = lead_raw.strip() if isinstance(lead_raw, str) and lead_raw.strip() else None
    sid_raw = form.get("submission_id")
    submission = FormSubmission(
        submission_id=_uuid_or_new(sid_raw if isinstance(sid_raw, str) else None),
        values=values,
        lead_token=lead_token,
        consent_email=str(form.get("consent_email") or "").lower() in {"yes", "on", "true", "1"},
    )
    errors = submission.errors
    if not values["address_line1"]:
        errors.append("Please enter the property's street address.")
    if not values["name"]:
        errors.append("Please enter your name.")
    if not values["email"] or not _EMAIL_RE.match(values["email"]):
        errors.append("Please enter a valid email address.")
    else:
        try:
            normalize_email(values["email"])
        except ValueError:
            errors.append("Please enter a valid email address.")
    if values["phone"]:
        try:
            normalize_phone(values["phone"])
        except ValueError:
            errors.append("Please enter a valid phone number, or leave it blank.")
    if values["state"] and len(values["state"]) != 2:
        errors.append("State should be the two-letter abbreviation (TX).")
    if values["condition_class"] and values["condition_class"] not in CONDITION_LABELS:
        errors.append("Please choose one of the listed condition options.")
    if values["timeline"] and values["timeline"] not in TIMELINE_LABELS:
        errors.append("Please choose one of the listed timeline options.")
    if values["asking_price"]:
        try:
            amount = Decimal(values["asking_price"].replace(",", "").replace("$", ""))
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errors.append("Asking price should be a number, or leave it blank.")
    if not submission.consent_email:
        errors.append("Please confirm we may contact you by email.")

    uploads = [u for u in form.getlist("photos") if isinstance(u, UploadFile)]
    uploads = [u for u in uploads if u.filename]
    if len(uploads) > settings.sell_form_max_photos:
        errors.append(f"Please upload at most {settings.sell_form_max_photos} photos.")
        uploads = uploads[: settings.sell_form_max_photos]
    limit = settings.sell_form_max_photo_bytes
    for upload in uploads:
        content_type = (upload.content_type or "").lower()
        if not content_type.startswith("image/"):
            errors.append(f"{upload.filename}: only image files are accepted.")
            continue
        data = await upload.read(limit + 1)
        if len(data) > limit:
            errors.append(f"{upload.filename}: larger than {limit // (1024 * 1024)} MB.")
            continue
        if not data:
            continue
        submission.photos.append(
            PhotoUpload(original_name=upload.filename or "", content_type=content_type, data=data)
        )
    return submission


# --------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------


def _common(settings: Settings) -> dict[str, Any]:
    return {
        "company": settings.sender_company,
        "sender_name": settings.sender_name,
        "postal_address": settings.sender_postal_address,
        "disclosure": disclosure(settings.sender_company),
    }


def _render_form(
    request: Request,
    templates: Any,
    settings: Settings,
    *,
    submission_id: str,
    lead_token: str | None,
    values: dict[str, str] | None = None,
    consent: bool = False,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    filled = {k: "" for k in TEXT_FIELDS}
    filled.update(values or {})
    context = {
        **_common(settings),
        "submission_id": submission_id,
        "lead_token": lead_token,
        "values": {**filled, "consent_email": consent},
        "errors": errors or [],
        "condition_classes": [{"value": k, "label": v} for k, v in CONDITION_LABELS.items()],
        "timelines": [{"value": k, "label": v} for k, v in TIMELINE_LABELS.items()],
        "max_photos": settings.sell_form_max_photos,
        "max_photo_mb": settings.sell_form_max_photo_bytes // (1024 * 1024),
    }
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request, "sell/form.html", context, status_code=status_code
    )


def _summary_body(values: dict[str, str], photo_keys: list[str]) -> str:
    lines = ["Web form submission"]
    for key in TEXT_FIELDS:
        if values.get(key):
            lines.append(f"{key}: {values[key]}")
    lines.append(f"photos: {len(photo_keys)}")
    lines.append("consent_email: yes")
    return "\n".join(lines)


async def _market_for(session: Any, city: str, state: str) -> uuid.UUID | None:
    if not city or not state:
        return None
    stmt = select(Market.id).where(Market.state == state.upper(), Market.name == city).limit(1)
    found = (await session.execute(stmt)).scalar_one_or_none()
    return found  # type: ignore[no-any-return]


# --------------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------------


@router.get("/sell", response_class=HTMLResponse)
async def sell_form(request: Request, settings: SettingsDep, templates: TemplatesDep) -> Response:
    if not get_rate_limiter(request).allow(client_ip(request)):
        return Response("Too many requests. Please try again in a minute.", status_code=429)
    lead_raw = request.query_params.get("lead")
    lead_token = lead_raw.strip() if lead_raw and lead_raw.strip() else None
    return _render_form(
        request, templates, settings, submission_id=str(uuid.uuid4()), lead_token=lead_token
    )


@router.post("/sell", response_class=HTMLResponse)
async def sell_submit(
    request: Request,
    settings: SettingsDep,
    templates: TemplatesDep,
    session: SessionDep,
    temporal: TemporalDep,
) -> Response:
    if not get_rate_limiter(request).allow(client_ip(request)):
        return Response("Too many requests. Please try again in a minute.", status_code=429)
    submission = await parse_submission(request, settings)
    if not submission.ok:
        return _render_form(
            request,
            templates,
            settings,
            submission_id=submission.submission_id,
            lead_token=submission.lead_token,
            values=submission.values,
            consent=submission.consent_email,
            errors=submission.errors,
            status_code=422,
        )
    sid = submission.submission_id
    values = submission.values
    repo = SellerRepository(session)
    audit = AuditRepository(session)

    # -- SEL-12 idempotency: the same submission id never produces a second lead ------------
    existing_lead = await repo.get_by_submission_id(sid)
    if existing_lead is not None or await audit.get_by_idempotency_key(f"form:{sid}:submitted"):
        intake = await repo.latest_intake(existing_lead.id) if existing_lead else None
        return _thanks(
            request,
            templates,
            settings,
            submission_id=sid,
            first_name=values["name"].split()[0] if values["name"] else None,
            property_address=values["address_line1"],
            photo_count=len(intake.photos) if intake else 0,
            missing=_quick_missing(values),
            duplicate=True,
        )

    # -- photos -> blob storage -------------------------------------------------------------
    blob = get_blob_storage_dep(request)
    now = datetime.now(tz=UTC)
    photo_records: list[dict[str, Any]] = []
    for i, photo in enumerate(submission.photos, start=1):
        key = f"sell/{sid}/{i:02d}.{_ext_for(photo)}"
        stored = await blob.put(key, photo.data, content_type=photo.content_type)
        photo_records.append(
            {
                "blob_key": stored.key,
                "uploaded_at": now.isoformat(),
                "content_type": stored.content_type,
                "size": stored.size,
                "sha256": stored.sha256,
                "original_name": photo.original_name[:255],
                "caption": None,
            }
        )

    # -- lead (create or merge) -------------------------------------------------------------
    state = values["state"].upper() if values["state"] else None
    address_key = normalize_address(
        values["address_line1"], values["city"] or None, state, values["postal_code"] or None
    )
    lead: SellerLead | None = None
    if submission.lead_token:
        try:
            lead = await repo.get(uuid.UUID(submission.lead_token))
        except ValueError:
            lead = None
    if lead is None:
        lead = await repo.find_existing(parcel_id=None, address_key=address_key)
    form_payload = {
        **values,
        "photos": [p["blob_key"] for p in photo_records],
        "consent_email": "yes",
    }
    if lead is None:
        lead = await repo.create_from_web_form(
            submission_id=sid,
            market_id=await _market_for(session, values["city"], state or ""),
            owner_name=values["name"] or None,
            address_line1=values["address_line1"],
            city=values["city"] or None,
            state=state,
            postal_code=values["postal_code"] or None,
            address_key=address_key,
            contact_email=values["email"],
            contact_phone=values["phone"] or None,
            source_refs={"web_form": {"submission_id": sid, "submitted_at": now.isoformat()}},
        )
        created = True
    else:
        created = False
        if lead.web_form_submission_id is None:
            lead.web_form_submission_id = sid
        if not lead.owner_name and values["name"]:
            lead.owner_name = values["name"]
        channels = list(lead.contact_channels or [])
        known_emails = {
            str(c.get("normalized_value") or "").lower()
            for c in channels
            if c.get("channel") == "email"
        }
        normalized_email = normalize_email(values["email"])
        if normalized_email not in known_emails:
            for c in channels:
                if c.get("channel") == "email":
                    c["is_primary"] = False
            channels.append(
                {
                    "channel": "email",
                    "value": values["email"],
                    "normalized_value": normalized_email,
                    "source": "web_form",
                    "provenance": f"seller-submitted (form {sid})",
                    "confidence": 1.0,
                    "is_primary": True,
                    "use_limitations": "seller consented to contact by email",
                }
            )
            lead.contact_channels = channels
        lead.source_refs = {
            **(lead.source_refs or {}),
            "web_form": {"submission_id": sid, "submitted_at": now.isoformat()},
        }
        await session.flush()

    thread = await repo.thread_for_lead(lead.id)
    if thread is None:
        thread = await repo.create_thread(
            lead=lead,
            channel=Channel.EMAIL,
            counterpart_address=values["email"],
            subject=f"Your property at {values['address_line1']}",
        )
    message, _ = await repo.create_form_message(
        thread=thread,
        submission_id=sid,
        from_address=values["email"],
        body=_summary_body(values, [p["blob_key"] for p in photo_records]),
        payload=form_payload,
        received_at=now,
    )
    intake = await repo.get_or_create_intake(lead)
    if photo_records:
        await repo.add_photos(intake, photo_records)
    await audit.record(
        actor=Actor.SYSTEM,
        actor_id="web_form",
        event_type=EVENT_SUBMITTED,
        entity_type="seller_lead",
        entity_id=lead.id,
        payload={
            "submission_id": sid,
            "lead_created": created,
            "intake_id": str(intake.id),
            "message_id": str(message.id),
            "photos": len(photo_records),
            "lead_token_used": submission.lead_token is not None,
        },
        correlation_id=sid,
        idempotency_key=f"form:{sid}:submitted",
    )
    await session.commit()

    # -- start the inbound workflow (idempotent on the submission id) ------------------------
    workflow_id = seller_inbound_workflow_id("form", sid)
    try:
        await temporal.start_workflow(
            SELLER_INBOUND_WORKFLOW_NAME,
            [
                SellerInboundInput(
                    source=SOURCE_WEB_FORM, message_id=str(message.id), submission_id=sid
                )
            ],
            workflow_id=workflow_id,
        )
    except Exception as exc:
        if not _already_started(exc):
            log.error("sell.workflow_start_failed", submission_id=sid, error=str(exc)[:300])
            await audit.record(
                actor=Actor.SYSTEM,
                actor_id="web_form",
                event_type=EVENT_START_FAILED,
                entity_type="seller_lead",
                entity_id=lead.id,
                payload={"submission_id": sid, "workflow_id": workflow_id, "error": str(exc)[:300]},
                correlation_id=sid,
                idempotency_key=f"form:{sid}:start_failed",
            )
            await session.commit()
    log.info(
        "sell.submitted",
        submission_id=sid,
        seller_lead_id=str(lead.id),
        lead_created=created,
        photos=len(photo_records),
        workflow_id=workflow_id,
    )
    return _thanks(
        request,
        templates,
        settings,
        submission_id=sid,
        first_name=values["name"].split()[0] if values["name"] else None,
        property_address=values["address_line1"],
        photo_count=len(photo_records),
        missing=_quick_missing(values),
        duplicate=False,
    )


def _quick_missing(values: dict[str, str]) -> list[str]:
    """What the thank-you page can already tell is missing (the parcel check runs in S4)."""
    return [k for k in ("motivation", "condition_class", "timeline") if not values.get(k)]


def _already_started(exc: Exception) -> bool:
    name = type(exc).__name__
    return name == "WorkflowAlreadyStartedError" or "already started" in str(exc).lower()


def _thanks(
    request: Request,
    templates: Any,
    settings: Settings,
    *,
    submission_id: str,
    first_name: str | None,
    property_address: str,
    photo_count: int,
    missing: list[str],
    duplicate: bool,
) -> Response:
    return templates.TemplateResponse(  # type: ignore[no-any-return]
        request,
        "sell/thanks.html",
        {
            **_common(settings),
            "submission_id": submission_id,
            "first_name": first_name,
            "property_address": property_address,
            "photo_count": photo_count,
            "missing": missing,
            "duplicate": duplicate,
        },
    )


__all__ = [
    "CONDITION_LABELS",
    "EVENT_SUBMITTED",
    "TIMELINE_LABELS",
    "FormSubmission",
    "IpRateLimiter",
    "PhotoUpload",
    "client_ip",
    "get_blob_storage_dep",
    "get_rate_limiter",
    "parse_submission",
    "router",
]
