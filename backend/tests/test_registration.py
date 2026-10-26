"""Emergency-contact registration in the patient's own words (FLOW-1, app/registration.py): typed in
any format or said in a voice note, read by Bedrock (keywords in mock mode); an emergency in the
same message is still triaged."""
import pytest

from app import pipeline
from app.registration import ContactDetails, RegistrationReader, keyword_extract, registration_reader, to_e164
from app.services.dynamo_service import dynamo_service
from test_pipeline_support import fakes, inbound, new_phone  # noqa: F401  (fixture import)

SENDER = "+923001112222"


def cases_for(phone):
    return [c for c in dynamo_service.list_all_cases() if c.user_phone == phone]


# ---------------------------------------------------------------- numbers
@pytest.mark.parametrize("raw,expected", [
    ("0333 1234567", "+923331234567"),
    ("+92 333 123-4567", "+923331234567"),
    ("00923331234567", "+923331234567"),
    ("۰۳۳۳۱۲۳۴۵۶۷", "+923331234567"),          # Urdu digits
    ("+919812345678", "+919812345678"),
    ("1122", None),
    ("call me", None),
])
def test_numbers_are_read_in_the_senders_country(raw, expected):
    assert to_e164(raw, SENDER) == expected


def test_local_numbers_follow_an_indian_sender():
    assert to_e164("098123 45678", "+919811111111") == "+919812345678"


# ---------------------------------------------------------------- keyword reader (mock mode)
@pytest.mark.asyncio
@pytest.mark.parametrize("text,patient,contact,phone", [
    ("Ahmed Khan, Brother Ali, +923331234567", "Ahmed Khan", "Brother Ali", "+923331234567"),
    ("Ahmed Khan, Brother Ali, 03331234567", "Ahmed Khan", "Brother Ali", "+923331234567"),
    ("Ahmed Khan\nBrother Ali\n+919812345678", "Ahmed Khan", "Brother Ali", "+919812345678"),
    ("my brother Ali 0333 1234567", None, "Brother Ali", "+923331234567"),
    ("Mera naam Ahmed hai, emergency contact mere bhai Ali ka number 0300 1234567 hai", "Ahmed", "Bhai Ali", "+923001234567"),
    ("My name is Ahmed Khan, contact my sister 0321 7654321", "Ahmed Khan", "Sister", "+923217654321"),
    ("۰۳۳۳۱۲۳۴۵۶۷ ammi", None, "Ammi", "+923331234567"),
    ("emergency contact is Sara Khan 0300 1234567", None, "Sara Khan", "+923001234567"),
])
async def test_free_form_registrations(text, patient, contact, phone):
    found = await registration_reader.read(text, SENDER)
    assert found.complete and not found.reports_emergency
    assert (found.patient_name, found.contact_name, found.contact_phone) == (patient, contact, phone)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "Help, my father collapsed, please hurry",
    "Accident, khoon beh raha hai, jaldi",
    "Chest pain, can't breathe, please help",
    "Mere abbu ko dil ka daura para hai, 03001234567 pe call karo",
    "Accident 03331234567",
])
async def test_emergencies_are_never_only_a_registration(text):
    found = await registration_reader.read(text, SENDER)
    assert found.reports_emergency


@pytest.mark.asyncio
async def test_registration_problems():
    assert (await registration_reader.read("emergency contact is my brother Ali", SENDER)).phone_problem == "missing"
    assert (await registration_reader.read("my brother's number is 0333 12", SENDER)).phone_problem == "invalid"
    assert (await registration_reader.read("my brother Ali 0300 1112222", SENDER)).phone_problem == "own_number"
    assert not (await registration_reader.read("seene me dard hai, saans nahi aa rahi, jaldi", SENDER)).is_registration


def test_a_relation_in_an_emergency_needs_a_contact_word():
    assert not keyword_extract("Mere abbu ko dil ka daura para hai, 03001234567 pe call karo").is_registration
    found = keyword_extract("I have chest pain, my wife number is 03001234567")
    assert found.is_registration and found.reports_emergency and found.contact_name == "Wife"


# ---------------------------------------------------------------- Bedrock reader (real AWS)
class ModelStub:
    def __init__(self, details=None, error=None):
        self.details, self.error, self.calls = details, error, []

    async def __call__(self, model_id, text):
        self.calls.append((model_id, text))
        if self.error:
            raise self.error
        return self.details


@pytest.mark.asyncio
async def test_model_reading_is_checked_and_normalised(monkeypatch):
    reader = RegistrationReader(use_models=True, model_factory=lambda model_id: None)
    stub = ModelStub(ContactDetails(is_registration=True, reports_emergency=False, patient_name="Ahmed",
                                    contact_name="Brother Ali", contact_phone="0333 1234567"))
    monkeypatch.setattr(reader, "_with_model", stub)
    found = await reader.read("mera naam Ahmed hai, bhai Ali ka number zero three three three ...", SENDER)
    assert found.complete and found.engine.startswith("bedrock:")
    assert (found.patient_name, found.contact_name, found.contact_phone) == ("Ahmed", "Brother Ali", "+923331234567")

    stub.details = ContactDetails(is_registration=True, reports_emergency=False, contact_phone="12")
    assert (await reader.read("my brother, number 12", SENDER)).phone_problem == "invalid"


@pytest.mark.asyncio
async def test_keywords_overrule_a_model_that_misses_an_emergency(monkeypatch):
    reader = RegistrationReader(use_models=True, model_factory=lambda model_id: None)
    monkeypatch.setattr(reader, "_with_model", ModelStub(ContactDetails(
        is_registration=True, reports_emergency=False, contact_name="Wife", contact_phone="03001234567")))
    found = await reader.read("seene me dard hai, my wife number is 03001234567", SENDER)
    assert found.complete and found.reports_emergency


@pytest.mark.asyncio
async def test_no_model_call_for_obvious_emergencies_or_the_comma_format(monkeypatch):
    reader = RegistrationReader(use_models=True, model_factory=lambda model_id: None)
    stub = ModelStub(error=AssertionError("the model must not be called"))
    monkeypatch.setattr(reader, "_with_model", stub)
    found = await reader.read("Accident ho gaya, khoon beh raha hai, emergency contact number 0333 1234567", SENDER)
    assert found.reports_emergency and found.complete and found.engine == "keywords"
    assert (await reader.read("Ahmed Khan, Brother Ali, 0333 1234567", SENDER)).complete
    assert stub.calls == []


@pytest.mark.asyncio
async def test_failed_models_fall_back_to_keywords(monkeypatch):
    reader = RegistrationReader(use_models=True, model_factory=lambda model_id: None)
    stub = ModelStub(error=RuntimeError("throttled"))
    monkeypatch.setattr(reader, "_with_model", stub)
    found = await reader.read("my brother Ali 0333 1234567", SENDER)
    assert found.complete and found.engine == "keywords"
    assert len(stub.calls) == len(reader.model_ids) >= 1


# ---------------------------------------------------------------- pipeline
@pytest.mark.asyncio
async def test_free_form_text_registers_without_a_case(fakes):
    phone = new_phone()
    await pipeline.process_inbound_message(inbound(phone, text="Hi", sender_name="Ahmed"))
    res = await pipeline.process_inbound_message(inbound(phone, text="my brother Ali 0333 1234567"))
    assert res["action"] == "registered" and res["case_id"] is None
    user = dynamo_service.get_user(phone)
    assert user.onboarding_status == "COMPLETE"
    assert (user.name, user.emergency_contact_name, user.emergency_contact_phone) == ("Ahmed", "Brother Ali", "+923331234567")
    assert "send_registration_confirmation" in fakes.whatsapp.names()
    assert cases_for(phone) == [] and fakes.triage.calls == []


@pytest.mark.asyncio
async def test_voice_note_registers_with_one_transcription(fakes):
    phone = new_phone()
    fakes.audio.transcription = fakes.audio.transcription.model_copy(update={
        "transcript": "My name is Ahmed. My emergency contact is my sister Sara, 0321 7654321.", "language": "English"})
    res = await pipeline.process_inbound_message(inbound(phone, type="audio"))
    assert res["action"] == "registered" and res["case_id"] is None
    assert len(fakes.audio.transcribe_calls) == 1
    user = dynamo_service.get_user(phone)
    assert (user.name, user.emergency_contact_name, user.emergency_contact_phone) == ("Ahmed", "Sister Sara", "+923217654321")
    assert cases_for(phone) == [] and fakes.triage.calls == []
    assert "send_onboarding_prompt" not in fakes.whatsapp.names()   # a new user who just registered


@pytest.mark.asyncio
async def test_emergency_with_a_contact_registers_then_triages_with_it(fakes):
    phone = new_phone()
    res = await pipeline.process_inbound_message(inbound(
        phone, type="audio",
        transcript="Seene me shadeed dard hai, saans nahi aa rahi. Emergency contact meri wife ka number 0300 7654321"))
    assert res["action"] == "triaged" and res["registered"] is True
    assert dynamo_service.get_user(phone).emergency_contact_phone == "+923007654321"
    assert fakes.triage.calls[0]["emergency_contact_phone"] == "+923007654321"
    assert "alert_emergency_contact" in fakes.triage.tools_fired
    names = fakes.whatsapp.names()
    # The emergency reply first, then the confirmation; no onboarding prompt.
    assert names.index("send_text_message") < names.index("send_registration_confirmation")
    assert "send_onboarding_prompt" not in names


@pytest.mark.asyncio
async def test_a_failing_registration_reader_never_blocks_triage(fakes, monkeypatch):
    async def broken(text, sender_phone):
        raise RuntimeError("reader exploded")
    monkeypatch.setattr(pipeline.registration_reader, "read", broken)
    phone = new_phone()
    res = await pipeline.process_inbound_message(inbound(
        phone, type="audio", transcript="Seene me dard hai, emergency contact number 0300 7654321"))
    assert res["action"] == "triaged" and len(fakes.triage.calls) == 1


@pytest.mark.asyncio
async def test_failed_transcription_is_still_a_human_review_case(fakes):
    phone = new_phone()
    fakes.audio.transcription = fakes.audio.transcription.model_copy(update={"ok": False, "transcript": "", "error": "boom"})
    res = await pipeline.process_inbound_message(inbound(phone, type="audio"))
    assert res["action"] == "human_review"
    assert len(fakes.audio.transcribe_calls) == 1


@pytest.mark.asyncio
async def test_registration_without_a_usable_number_asks_for_it(fakes):
    phone = new_phone()
    await pipeline.process_inbound_message(inbound(phone, text="hello"))
    res = await pipeline.process_inbound_message(inbound(phone, text="emergency contact is my brother Ali"))
    assert res["action"] == "registration_hint"
    hint = [c for c in fakes.whatsapp.calls if c[0] == "send_registration_format_hint"][-1]
    assert hint[1][1] == "missing"
    assert dynamo_service.get_user(phone).onboarding_status == "PENDING_DETAILS"
    assert fakes.triage.calls == [] and cases_for(phone) == []


@pytest.mark.asyncio
async def test_open_case_voice_note_with_a_contact_is_noted_for_the_doctor(fakes):
    phone = new_phone()
    first = await pipeline.process_inbound_message(inbound(phone, type="audio", transcript="dil ka daura"))
    res = await pipeline.process_inbound_message(inbound(phone, type="audio", transcript="mera bhai Ali 0333 1234567"))
    assert res["action"] == "registered" and res["case_id"] == first["case_id"]
    case = dynamo_service.get_case(first["case_id"])
    assert case.emergency_contact_phone == "+923331234567"
    assert any("Brother Ali" in m.content or "Bhai Ali" in m.content
               for m in dynamo_service.get_case_messages(case.case_id) if m.sender == "BOT")
