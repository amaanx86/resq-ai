"""Emergency-contact registration in the patient's own words (FLOW-1).

A patient registers their emergency contact however they like: typed in any order or language
("my brother Ali 0333 1234567", "Ahmed here, emergency contact Sara +92 300 1234567") or said in a
voice note (Amazon Transcribe turns it into text first, in `app.pipeline`). This module reads the
text and returns what it found.

1. The old strict format `Name, Contact Name, +E164` is still read directly, without a model.
2. Otherwise only a message that could be a registration is examined (`worth_checking`: it has a
   phone-like number, or it talks about a contact), so ordinary emergency messages never wait for
   an extra model call.
3. Real AWS: a Bedrock model (`BEDROCK_MODEL_ID`, then `BEDROCK_FALLBACK_MODEL_ID`, Strands
   structured output) extracts the patient's name, the contact's name or relation and the number,
   and says whether the message ALSO reports an emergency. Mock / Floci, or every model failed: the
   keyword reader below (`keyword_extract`).
4. The number is checked and turned into E.164 with `phonenumbers`, local numbers ("0333 1234567")
   in the sender's own country. Spoken digits in Urdu / Hindi script are read too.

The pipeline registers a valid contact, and triages the message as well whenever it reports an
emergency (by the model's judgement OR the keyword classifier's ESI <= 3), so an emergency is never
swallowed by registration.
"""
import asyncio
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

import phonenumbers
from pydantic import BaseModel, Field

from app.agents.core import default_model_factory, heuristic_clinical_eval
from app.config import settings

DEFAULT_CONTACT_NAME = "Emergency contact"
EXTRACT_TIMEOUT_SECONDS = 15

# Urdu (Extended Arabic-Indic), Arabic-Indic and Devanagari digits -> ASCII.
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩०१२३४५६७८९", "012345678901234567890123456789")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-().]{6,}\d")
_PARTIAL_PHONE_RE = re.compile(r"\+?\d[\d\s\-]*\d[\d\s\-]*\d")  # 3+ digits

# A message that talks about an emergency contact (English, Roman Urdu / Hindi, Urdu, Hindi).
_CONTACT_WORDS = re.compile(
    r"\b(emergency contact|contact|number|nambar|numbar|phone|mobile|register|registration)\b"
    r"|رابطہ|رابطے|نمبر|ایمرجنسی|کانٹیکٹ|संपर्क|नंबर|नम्बर|इमरजेंसी|कॉन्टैक्ट",
    re.IGNORECASE)

_RELATIONS = ("brother", "sister", "father", "mother", "dad", "mom", "wife", "husband", "son", "daughter",
              "friend", "uncle", "aunt", "cousin", "neighbour", "neighbor", "bhai", "bhaiya", "behan", "bahen",
              "baji", "abbu", "abba", "ammi", "ammu", "walid", "walida", "beta", "beti", "shohar", "biwi",
              "begum", "dost", "chacha", "mamu", "mamoo", "khala", "phuppo", "papa", "mama", "didi")
# Relation words in any case; a name after them only when capitalised ("bhai Ali", not "abbu ko").
_RELATION_RE = re.compile(r"\b(?i:(" + "|".join(_RELATIONS) + r"))\b(?:\s+([A-Z][a-zA-Z]{1,30}))?")
_CONTACT_NAME_RE = re.compile(r"\bcontact(?:\s+(?:is|name|naam))?\s*[:\-]?\s+([A-Z][a-zA-Z]{1,30}(?:\s+[A-Z][a-zA-Z]{1,30})?)")
_PATIENT_NAME_RE = re.compile(
    r"\b(?i:my name is|my name's|i am|i'm|this is|mera naam|mera nam)\s+([A-Z][a-zA-Z]{1,30}(?:\s+[A-Z][a-zA-Z]{1,30})?)")
_NOT_NAMES = {"hai", "he", "ha", "and", "aur", "contact", "emergency", "here", "number", "not", "in", "at",
              "ko", "ka", "ki", "ke", "se", "pe", "par", "mere", "meri", "mera"}


class ContactDetails(BaseModel):
    """What the model reads from the patient's message."""
    is_registration: bool = Field(description="True if the message gives, changes or corrects the patient's "
                                              "emergency contact (someone to call for them in an emergency).")
    reports_emergency: bool = Field(description="True if the message also describes a medical problem, injury, "
                                                "symptom or any request for urgent help.")
    patient_name: Optional[str] = Field(default=None, description="The patient's own name, only if they say it.")
    contact_name: Optional[str] = Field(default=None, description="The contact's name and/or relation as said, "
                                                                  "e.g. 'Brother Ali', 'Ammi', 'Sara'.")
    contact_phone: Optional[str] = Field(default=None, description="The contact's phone number exactly as given, "
                                                                   "spoken number words converted to digits.")


SYSTEM_PROMPT = """You read WhatsApp messages sent to ResQ-AI, an emergency medical service in Pakistan and India.
New users are asked to tell us their emergency contact: a person we call if they report an emergency.
Users write or speak (voice notes, transcribed by Amazon Transcribe) in English, Urdu, Hindi, or Roman Urdu/Hindi,
in any order and format. Transcripts may contain transcription errors, and numbers may be spoken as words
("zero three three three..." or in Urdu/Hindi); convert those to digits.

Extract:
- is_registration: the message gives, changes or corrects the user's emergency contact.
- reports_emergency: the message ALSO describes a medical problem, injury, symptoms or asks for urgent help.
  When unsure, say true: an emergency must never be missed.
- patient_name: the user's own name, only if stated.
- contact_name: the contact's name and/or relation in the user's words (translate relation words to English,
  e.g. "Bhai Ali" -> "Brother Ali", "Ammi" -> "Mother").
- contact_phone: the contact's phone number as digits (keep a leading + or country code if given).
Never invent a name or number that is not in the message."""


@dataclass
class Registration:
    """The result for one message. `contact_phone` is E.164 when a valid number was found."""
    is_registration: bool
    reports_emergency: bool
    patient_name: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    # Why there is no usable number for a registration: "missing", "invalid" or "own_number".
    phone_problem: Optional[str] = None
    engine: str = "keywords"

    @property
    def complete(self) -> bool:
        return self.is_registration and bool(self.contact_phone)


def normalize_digits(text: str) -> str:
    return (text or "").translate(_DIGITS)


def worth_checking(text: str) -> bool:
    """Could this message register a contact? Only these go to the model."""
    text = normalize_digits(text)
    return bool(_PHONE_RE.search(text) or _CONTACT_WORDS.search(text)
                or (_RELATION_RE.search(text) and _PARTIAL_PHONE_RE.search(text)))


def to_e164(raw: Optional[str], sender_phone: str) -> Optional[str]:
    """A valid phone number in E.164, local numbers read in the sender's country; else None."""
    digits = normalize_digits(raw or "").strip()
    if not re.search(r"\d", digits):
        return None
    try:
        region = phonenumbers.region_code_for_number(phonenumbers.parse(sender_phone, None))
    except phonenumbers.NumberParseException:
        region = None
    try:
        number = phonenumbers.parse(digits, region or "PK")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def _clean_name(name: Optional[str]) -> Optional[str]:
    name = re.sub(r"\s+", " ", (name or "")).strip(" ,.:;-")
    if not name or re.search(r"\d", name) or len(name) > 80:
        return None
    return name


def _keyword_esi(text: str) -> int:
    try:
        return heuristic_clinical_eval(_PHONE_RE.sub(" ", text), "Unknown").esi_level
    except Exception as e:
        print(f"[Registration] Keyword classifier failed: {e}")
        return 1   # unsure: treat it as an emergency


def _comma_format(text: str, emergency: bool) -> Optional[ContactDetails]:
    """"Name, Contact, number" or "Contact, number": the old format, with any number style."""
    parts = [p.strip() for p in re.split(r"[,\n;]", text) if p.strip()]
    if len(parts) not in (2, 3) or not _PHONE_RE.fullmatch(parts[-1]):
        return None
    names = [_clean_name(p) for p in parts[:-1]]
    if not all(names):
        return None
    patient, contact = (names[0], names[1]) if len(names) == 2 else (None, names[0])
    return ContactDetails(is_registration=True, reports_emergency=emergency,
                          patient_name=patient, contact_name=contact, contact_phone=parts[-1])


def keyword_extract(text: str) -> ContactDetails:
    """Model-free reading (mock / Floci mode, obvious emergencies, and when every Bedrock model fails)."""
    text = normalize_digits(text)
    phones = _PHONE_RE.findall(text)
    emergency = _keyword_esi(text) <= 3
    patient = contact = None
    phone = phones[0] if phones else None
    comma = _comma_format(text, emergency)
    if comma:
        return comma

    m = _PATIENT_NAME_RE.search(text)
    if m:
        words = [w for w in m.group(1).split() if w.lower() not in _NOT_NAMES]
        patient = _clean_name(" ".join(words)) if words else None
    m = _RELATION_RE.search(text)
    if m:
        relation, name = m.group(1), m.group(2)
        if name and name.lower() in _NOT_NAMES:
            name = None
        contact = _clean_name(f"{relation.capitalize()} {name}" if name else relation.capitalize())
    else:
        m = _CONTACT_NAME_RE.search(text)
        if m:
            contact = _clean_name(m.group(1))

    # In an emergency a relation word alone ("my father collapsed, call 0300...") may be the patient,
    # so it then takes an explicit contact / number word to count as a registration.
    says_contact = bool(_CONTACT_WORDS.search(text))
    short = len(text.split()) <= 12
    partial = _PARTIAL_PHONE_RE.search(text)
    is_registration = bool(phone or says_contact or (contact and partial)) and (
        says_contact or (not emergency and (bool(contact) or (short and bool(phone)))))
    if is_registration and not phone and partial:
        # Too few digits for a phone number: passed on, so the patient is told it looks incomplete.
        phone = partial.group(0)
    return ContactDetails(is_registration=is_registration, reports_emergency=emergency,
                          patient_name=patient, contact_name=contact, contact_phone=phone)


class RegistrationReader:
    """Reads registrations; `use_models=False` (mock / Floci) never calls Bedrock."""

    def __init__(self, use_models: bool, model_factory: Optional[Callable[[str], Any]] = None):
        self.use_models = use_models
        self.model_factory = model_factory or default_model_factory(settings.AWS_REGION)
        self.model_ids = [m for m in dict.fromkeys((settings.BEDROCK_MODEL_ID, settings.BEDROCK_FALLBACK_MODEL_ID)) if m]

    async def _with_model(self, model_id: str, text: str) -> ContactDetails:
        from strands import Agent
        agent = Agent(model=self.model_factory(model_id), system_prompt=SYSTEM_PROMPT, tools=[],
                      callback_handler=None, name="resq-registration")
        result = await asyncio.wait_for(
            agent.invoke_async(f'Message: "{text}"', structured_output_model=ContactDetails),
            timeout=EXTRACT_TIMEOUT_SECONDS)
        details = getattr(result, "structured_output", None)
        if isinstance(details, ContactDetails):
            return details
        return ContactDetails.model_validate(details.model_dump())

    async def _details(self, text: str, keyword_esi: int):
        # No model call for the old comma format, nor for an obvious emergency (ESI 1-2 by keywords),
        # which must not wait for one; the keyword reader still saves an explicit contact in it.
        if self.use_models and keyword_esi > 2 and not _comma_format(text, False):
            for model_id in self.model_ids:
                try:
                    return await self._with_model(model_id, text), f"bedrock:{model_id}"
                except Exception as e:
                    print(f"[Registration] Model {model_id} failed: {type(e).__name__}: {e}")
            print("[Registration] All Bedrock models failed; using the keyword reader.")
        return keyword_extract(text), "keywords"

    async def read(self, text: str, sender_phone: str) -> Registration:
        text = normalize_digits(text)
        keyword_esi = _keyword_esi(text)
        details, engine = await self._details(text, keyword_esi)
        # Never let registration hide an emergency: the keyword classifier can only add one.
        emergency = bool(details.reports_emergency) or keyword_esi <= 3
        result = Registration(is_registration=bool(details.is_registration), reports_emergency=emergency,
                              patient_name=_clean_name(details.patient_name),
                              contact_name=_clean_name(details.contact_name), engine=engine)
        if not result.is_registration:
            return result
        if not details.contact_phone:
            result.phone_problem = "missing"
            return result
        phone = to_e164(details.contact_phone, sender_phone)
        if phone is None:
            result.phone_problem = "invalid"
        elif phone == to_e164(sender_phone, sender_phone):
            result.phone_problem = "own_number"
        else:
            result.contact_phone = phone
        return result


registration_reader = RegistrationReader(use_models=not (settings.MOCK_AWS or bool(settings.AWS_ENDPOINT_URL)))
