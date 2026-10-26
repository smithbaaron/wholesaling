"""Mock DocuSign gateway: envelope state machine, idempotency, void rules, Connect payloads."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from wholesaling.config.settings import Settings
from wholesaling.integrations.docusign import (
    DocuSignInvalidTransition,
    DocuSignNotFound,
    DocuSignTemplateUnconfigured,
    EnvelopeRequest,
    EnvelopeStatus,
    MockDocuSignGateway,
    TemplateRecipient,
    get_docusign,
    template_id_for,
    verify_connect_signature,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _req(
    key: str | None = "psa:1", status: EnvelopeStatus = EnvelopeStatus.CREATED
) -> EnvelopeRequest:
    return EnvelopeRequest(
        template_key="psa",
        template_id="mock-template-psa",
        email_subject="PSA",
        recipients=[TemplateRecipient(role_name="Seller", name="M", email="m@x.com")],
        status=status,
        idempotency_key=key,
    )


@pytest.fixture
def gw() -> MockDocuSignGateway:
    return MockDocuSignGateway(clock=FakeClock())


async def test_healthcheck_and_templates(gw: MockDocuSignGateway) -> None:
    health = await gw.healthcheck()
    assert health.ok and health.mode == "mock" and health.name == "docusign"
    templates = await gw.list_templates()
    assert {t.template_id for t in templates} == {"mock-template-psa", "mock-template-assignment"}
    assert [c.method for c in gw.calls] == ["healthcheck", "list_templates"]


async def test_create_envelope_is_a_draft_by_default_and_idempotent(
    gw: MockDocuSignGateway,
) -> None:
    first = await gw.create_envelope_from_template(_req())
    again = await gw.create_envelope_from_template(_req())
    assert first.status is EnvelopeStatus.CREATED
    assert first.envelope_id == again.envelope_id
    assert len(gw.envelopes) == 1
    assert first.raw["payload"]["templateId"] == "mock-template-psa"
    other = await gw.create_envelope_from_template(_req(key="psa:2"))
    assert other.envelope_id != first.envelope_id


async def test_lifecycle_created_sent_delivered_completed(gw: MockDocuSignGateway) -> None:
    info = await gw.create_envelope_from_template(_req())
    eid = info.envelope_id
    assert gw.send(eid).status is EnvelopeStatus.SENT
    assert gw.deliver(eid).status is EnvelopeStatus.DELIVERED
    assert gw.complete(eid).status is EnvelopeStatus.COMPLETED
    status = await gw.get_envelope_status(eid)
    assert status.status is EnvelopeStatus.COMPLETED and status.status_at is not None
    with pytest.raises(DocuSignInvalidTransition):
        gw.advance(eid, EnvelopeStatus.SENT)  # terminal


async def test_complete_from_created_passes_through_sent(gw: MockDocuSignGateway) -> None:
    info = await gw.create_envelope_from_template(_req())
    assert gw.complete(info.envelope_id).status is EnvelopeStatus.COMPLETED


async def test_decline_is_terminal(gw: MockDocuSignGateway) -> None:
    info = await gw.create_envelope_from_template(_req())
    assert gw.decline(info.envelope_id).status is EnvelopeStatus.DECLINED
    with pytest.raises(DocuSignInvalidTransition):
        gw.complete(info.envelope_id)


async def test_void_requires_reason_and_in_process_envelope(gw: MockDocuSignGateway) -> None:
    info = await gw.create_envelope_from_template(_req(status=EnvelopeStatus.SENT))
    with pytest.raises(DocuSignInvalidTransition):
        await gw.void_envelope(info.envelope_id, "   ")
    voided = await gw.void_envelope(info.envelope_id, "seller backed out")
    assert voided.status is EnvelopeStatus.VOIDED and voided.voided_reason == "seller backed out"
    done = await gw.create_envelope_from_template(_req(key="psa:3"))
    gw.complete(done.envelope_id)
    with pytest.raises(DocuSignInvalidTransition):
        await gw.void_envelope(done.envelope_id, "too late")  # completed envelopes do not void


async def test_unknown_envelope_raises_not_found(gw: MockDocuSignGateway) -> None:
    with pytest.raises(DocuSignNotFound):
        await gw.get_envelope_status("nope")
    with pytest.raises(DocuSignNotFound):
        await gw.void_envelope("nope", "x")


async def test_create_with_terminal_status_is_refused(gw: MockDocuSignGateway) -> None:
    with pytest.raises(DocuSignInvalidTransition):
        await gw.create_envelope_from_template(_req(status=EnvelopeStatus.COMPLETED))


async def test_nothing_in_the_mock_signs_on_its_own(gw: MockDocuSignGateway) -> None:
    """Time passing never moves an envelope: only an explicit advance does (POL-03i)."""
    clock = FakeClock()
    gw = MockDocuSignGateway(clock=clock)
    info = await gw.create_envelope_from_template(_req())
    clock.now += timedelta(days=30)
    assert (await gw.get_envelope_status(info.envelope_id)).status is EnvelopeStatus.CREATED


async def test_connect_event_and_signed_delivery(gw: MockDocuSignGateway) -> None:
    info = await gw.create_envelope_from_template(_req())
    gw.complete(info.envelope_id)
    event = gw.connect_event_for(info.envelope_id, retry_count=2)
    assert event.event == "envelope-completed"
    assert event.envelope_id == info.envelope_id
    assert event.retry_count == 2
    assert event.envelope_status is EnvelopeStatus.COMPLETED
    body, headers = gw.connect_delivery(info.envelope_id)
    assert json.loads(body)["data"]["envelopeSummary"]["status"] == "completed"
    assert verify_connect_signature(body, gw.connect_hmac_key, headers["X-DocuSign-Signature-1"])


async def test_reset_clears_state(gw: MockDocuSignGateway) -> None:
    await gw.create_envelope_from_template(_req())
    gw.reset()
    assert not gw.envelopes and not gw.calls


def test_factory_defaults_to_mock_and_template_ids() -> None:
    settings = Settings(_env_file=None, app_env="test")  # type: ignore[call-arg]
    assert settings.docusign_mode == "mock"
    gw = get_docusign(settings)
    assert isinstance(gw, MockDocuSignGateway)
    assert template_id_for(settings, "psa") == "mock-template-psa"
    assert template_id_for(settings, "assignment") == "mock-template-assignment"
    configured = Settings(_env_file=None, app_env="test", docusign_psa_template_id="real-id")  # type: ignore[call-arg]
    assert template_id_for(configured, "psa") == "real-id"


def test_live_mode_without_template_id_is_refused() -> None:
    settings = Settings(_env_file=None, app_env="test", docusign_mode="live")  # type: ignore[call-arg]
    with pytest.raises(DocuSignTemplateUnconfigured):
        template_id_for(settings, "psa")
