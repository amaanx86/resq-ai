"""Escalating an existing case moves its monthly METRIC counters to the new ESI bucket."""
import uuid

from app.models.schemas import EmergencyCase, ChatMessage
from app.services.dynamo_service import dynamo_service


def _counters(month):
    return dict(dynamo_service.get_month_metrics(month) or {})


def test_escalation_moves_case_between_esi_buckets():
    case = EmergencyCase(
        case_id=f"CASE-TEST-{uuid.uuid4().hex[:8]}", user_phone="+923000009999", patient_name="Test",
        transcript="ulti aur pet me dard", detected_language="Urdu", esi_level=3, severity="MODERATE",
        status="ACTIVE", matched_specialty="General Emergency",
    )
    month = case.created_at[:7]
    before = _counters(month)
    dynamo_service.save_case(case)
    after_create = _counters(month)
    assert after_create.get("esi_3", 0) == before.get("esi_3", 0) + 1

    dynamo_service.update_case(case.case_id, {"esi_level": 1, "severity": "CRITICAL", "status": "DOCTOR_HANDOVER",
                                              "matched_specialty": "Cardiology"})
    after_escalation = _counters(month)
    assert after_escalation.get("esi_3", 0) == before.get("esi_3", 0)
    assert after_escalation.get("esi_1_2", 0) == after_create.get("esi_1_2", 0) + 1
    assert after_escalation.get("cases_total") == after_create.get("cases_total")
    assert after_escalation.get("spec#Cardiology", 0) == after_create.get("spec#Cardiology", 0) + 1
    assert after_escalation.get("spec#General Emergency", 0) == after_create.get("spec#General Emergency", 0) - 1

    # Fields that don't feed the counters leave them alone.
    dynamo_service.update_case(case.case_id, {"reasoning": "updated"})
    assert _counters(month) == after_escalation


def test_in_memory_chat_save_replaces_same_message_id():
    case_id = f"CASE-TEST-{uuid.uuid4().hex[:8]}"
    msg = ChatMessage(message_id="MSG-FIXED", case_id=case_id, sender="BOT", sender_name="ResQ-AI", content="hi")
    dynamo_service.save_chat_message(msg)
    dynamo_service.save_chat_message(msg)
    assert len(dynamo_service.get_case_messages(case_id)) == 1
