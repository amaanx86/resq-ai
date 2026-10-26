"""The doctor's "@resq" commands on WhatsApp (DOC-4), and the DOC:* buttons.

A message from a doctor that starts with "@resq" (case-insensitive, "@resq:" too) is never relayed to
the patient. Fixed commands are matched without a model; anything else goes to the doctor agent
(`app.agents.doctor_agent`, DOC-5), which can only run these same commands. Every command acts on the
doctor's one case (OFFER-4) and is recorded in the case chat, so the portal shows it.

| Command | Does |
|---|---|
| `@resq`, `@resq help`, `@resq menu` | the actions as a WhatsApp list |
| `@resq video` | video call: the patient gets the invite, the doctor their own link (no portal login) |
| `@resq location` | asks the patient for their location again |
| `@resq ambulance called` | asks to confirm, then records it and tells the patient |
| `@resq summary` | the case card again |
| `@resq close` | asks to confirm, then resolves the case (the doctor is free again) |
| `@resq hand over` | asks to confirm, then gives the case back to the offer rounds, without them |
| `@resq call` | an Open chat & call link (the patient will see the doctor's number) |
| `@resq accept` / `@resq decline` | answers the case offered to them |
| `@resq emergency <what is happening>` | the doctor's own emergency: handled as a patient's message |

Buttons (`DOC:...` payloads, `pipeline.DOCTOR_ACTIONS["DOC"]`): `DOC:CMD:<command>` (menu rows),
`DOC:CONFIRM:<command>:<case>` / `DOC:CANCEL:<case>` (confirmations; the assignment is checked again
at tap time) and `DOC:SHOW:<case>` (the DOC-6 template: the messages the doctor missed).
"""
import re
import uuid
from typing import Any, Dict, List, Optional

from app import doctor_chat
from app.agents.doctor_agent import COMMAND_DESCRIPTIONS, doctor_agent
from app.models.schemas import ChatMessage, DoctorProfile, EmergencyCase, InboundWhatsAppMessage
from app.services import events_service
from app.services.dynamo_service import OPEN_CASE_STATUSES, dynamo_service

PREFIX_RE = re.compile(r"^\s*@\s*resq\b\s*[:,\-]?\s*", re.IGNORECASE)
CONFIRMED_COMMANDS = ("ambulance", "close", "handover")
NEEDS_CASE = ("video", "location", "ambulance", "summary", "close", "handover", "call")

# Fixed phrases -> command; the whole message, or its first words (longest phrase first).
PHRASES: Dict[str, tuple] = {
    "help": ("help", "menu", "commands", "options", "actions", "?"),
    "video": ("video", "video call", "videocall", "start video", "start video call", "start a video call", "vc"),
    "location": ("location", "send location", "ask location", "ask for location", "request location"),
    "ambulance": ("ambulance called", "ambulance", "called ambulance", "i called an ambulance", "1122 called"),
    "summary": ("summary", "case summary", "case", "details", "info"),
    "close": ("close", "close case", "close the case", "resolve", "done", "finish", "end case"),
    "handover": ("hand over", "handover", "hand-over", "hand over case", "release", "transfer"),
    "call": ("call", "call patient", "call the patient", "phone"),
    "accept": ("accept", "accept case"),
    "decline": ("decline", "decline case", "reject"),
    "emergency": ("emergency", "sos"),
}
_PHRASE_LIST = sorted(((p, c) for c, ps in PHRASES.items() for p in ps), key=lambda pc: -len(pc[0]))

MENU_ROWS = [
    ("video", "Video call", "Send the patient a video link; you get yours"),
    ("location", "Ask for location", "Ask the patient to share their location"),
    ("ambulance", "Ambulance called", "Record that you called an ambulance"),
    ("summary", "Case summary", "Triage, history, photo, location"),
    ("call", "Call on WhatsApp", "Call from your own number (they see it)"),
    ("handover", "Hand over", "Give the case to another doctor"),
    ("close", "Close case", "You are done with this case"),
]


def command_text(text: str) -> Optional[str]:
    """The text after "@resq" ("" for "@resq" alone), or None when it is not a command."""
    match = PREFIX_RE.match(text or "")
    return text[match.end():].strip() if match else None


POLITE_WORDS = ("please", "pls", "plz", "now")


def parse_command(text: str) -> Optional[str]:
    """A fixed command ("video", "close", ...) or None (free-form: the doctor agent). Only the exact
    phrase, optionally with "please" / "now", counts: anything longer may mean something else."""
    norm = re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .!")
    if not norm:
        return "help"
    words = [w for w in norm.split(" ") if w not in POLITE_WORDS]
    core = " ".join(words)
    for phrase, command in _PHRASE_LIST:
        if core == phrase and command != "emergency":
            return command
    return None


def emergency_text(text: str) -> Optional[str]:
    """"emergency <what is happening>" -> what is happening ("" for the word alone); None otherwise."""
    match = re.match(r"^\s*(emergency|sos)\b[:,\-]?\s*(.*)$", text or "", re.IGNORECASE | re.DOTALL)
    return match.group(2).strip() if match else None


async def _note(case_id: str, text: str) -> None:
    try:
        msg = dynamo_service.save_chat_message(ChatMessage(
            message_id=f"MSG-{uuid.uuid4().hex[:8]}", case_id=case_id, sender="BOT", sender_name="System",
            content=text))
        await events_service.publish_chat_message(msg)
    except Exception as e:
        print(f"[DoctorCommands] Chat note for {case_id} failed: {e}")


async def _tell(doctor: DoctorProfile, text: str) -> Dict[str, Any]:
    return await doctor_chat.tell_doctor(doctor, text)


def _case_info(case: Optional[EmergencyCase], offered: Optional[EmergencyCase]) -> Optional[Dict[str, Any]]:
    """The case as structured fields for the doctor agent (no patient messages)."""
    if case is not None:
        return {"status": "assigned to this doctor", "patient name": case.patient_name, "ESI level": case.esi_level,
                "specialty": case.matched_specialty or "General Emergency",
                "location shared": bool(case.location_shared_at), "ambulance recorded": bool(case.ambulance_called_at)}
    if offered is not None:
        return {"status": "offered to this doctor, not accepted yet", "ESI level": offered.esi_level,
                "specialty": offered.matched_specialty or "General Emergency"}
    return None


# ---------------------------------------------------------------------------- entry points
async def handle(msg: InboundWhatsAppMessage, doctor: DoctorProfile, text: str) -> Dict[str, Any]:
    """One "@resq ..." message (the text after the prefix)."""
    command = parse_command(text)
    if command is not None:
        reply = await run(command, doctor, msg)
        return {"action": f"doctor_command_{command}", "case_id": reply.get("case_id")}

    case, offered = doctor_chat.doctor_case(doctor), None
    if case is None:
        offered = doctor_chat.offered_case(doctor)
    results: List[Dict[str, Any]] = []

    async def run_tool(name: str) -> Optional[str]:
        result = await run(name, doctor, msg)
        results.append(result)
        return result.get("summary")

    outcome = await doctor_agent.handle(text, doctor.name, _case_info(case, offered), run_tool)
    print(f"[DoctorCommands] {doctor.doctor_id} free-form command -> {outcome.executed or 'nothing'} ({outcome.engine}).")
    if not outcome.executed:
        if outcome.reply:
            await _tell(doctor, outcome.reply)
        else:
            await _tell(doctor, "Sorry, I didn't understand that. Here is what I can do:")
            await send_menu(doctor, case)
        return {"action": "doctor_command_unclear", "case_id": case.case_id if case else None}
    if outcome.reply and outcome.engine.startswith("bedrock"):
        await _tell(doctor, outcome.reply)
    case_ids = [r.get("case_id") for r in results if r.get("case_id")]
    return {"action": "doctor_command_agent", "case_id": case_ids[0] if case_ids else None,
            "commands": outcome.executed}


async def handle_button(msg: InboundWhatsAppMessage, doctor: DoctorProfile, parts: List[str]) -> Dict[str, Any]:
    """`DOC:...` from a doctor's WhatsApp (pipeline.DOCTOR_ACTIONS)."""
    kind = parts[0] if parts else ""
    if kind == "CMD" and len(parts) >= 2 and parts[1] in COMMAND_DESCRIPTIONS:
        result = await run(parts[1], doctor, msg)
        return {"action": f"doctor_command_{parts[1]}", "case_id": result.get("case_id")}
    if kind == "CONFIRM" and len(parts) == 3 and parts[1] in CONFIRMED_COMMANDS:
        return await _confirmed(parts[1], parts[2], doctor)
    if kind == "CANCEL":
        await _tell(doctor, "OK, nothing was changed.")
        return {"action": "doctor_command_cancelled", "case_id": parts[1] if len(parts) > 1 else None}
    if kind == "SHOW" and len(parts) == 2:
        case = dynamo_service.get_case(parts[1])
        if case is None or case.assigned_doctor_id != doctor.doctor_id or case.status not in OPEN_CASE_STATUSES:
            await _tell(doctor, "This case is no longer yours or is closed.")
            return {"action": "doctor_show_stale", "case_id": parts[1]}
        count = await doctor_chat.flush_missed(case, doctor)
        if not count:
            await _tell(doctor, f"No new messages from {case.patient_name}. Reply here to talk to them.")
        return {"action": "doctor_show_missed", "case_id": case.case_id, "count": count}
    await _tell(doctor, "This button is no longer active. Send *@resq help* for the actions.")
    return {"action": "doctor_button_unknown", "case_id": None}


# ---------------------------------------------------------------------------- commands
async def send_menu(doctor: DoctorProfile, case: Optional[EmergencyCase]) -> Dict[str, Any]:
    if case is None:
        offered = doctor_chat.offered_case(doctor)
        text = (f"A case is offered to you ({offered.patient_name}, ESI {offered.esi_level}): send *@resq accept* or "
                "*@resq decline*." if offered else
                "You have no open case right now. New cases are offered to you here on WhatsApp.")
        return await _tell(doctor, text)
    rows = [{"id": f"{doctor_chat.DOCTOR_PREFIX}:CMD:{c}", "title": t, "description": d} for c, t, d in MENU_ROWS]
    return await doctor_chat._send(
        "send_list_message", doctor.phone_number,
        f"Case: {case.patient_name}, ESI {case.esi_level}. Choose an action, or type it after @resq "
        "(e.g. @resq video). Anything without @resq goes to the patient.", "Actions", rows, header="ResQ-AI actions")


async def run(command: str, doctor: DoctorProfile, msg: Optional[InboundWhatsAppMessage] = None) -> Dict[str, Any]:
    """Runs one command for the doctor's case. Returns {"case_id", "summary"} (the summary is what the
    doctor agent sees: never patient text)."""
    if command == "help":
        case = doctor_chat.doctor_case(doctor)
        await send_menu(doctor, case)
        return {"case_id": case.case_id if case else None, "summary": "The menu was sent."}
    if command in ("accept", "decline"):
        return await _answer_offer(command, doctor, msg)
    if command == "emergency":
        await _tell(doctor, "To report your own emergency, send *@resq emergency* followed by what is happening, "
                            "or send a voice note when you have no open case.")
        return {"case_id": None, "summary": "Explained how to report an emergency."}
    case = doctor_chat.doctor_case(doctor)
    if case is None:
        await send_menu(doctor, None)
        return {"case_id": None, "summary": "The doctor has no open case."}
    if command in CONFIRMED_COMMANDS:
        return await _ask_confirmation(command, case, doctor)
    handler = {"video": _video, "location": _location, "summary": _summary, "call": _call}.get(command)
    if handler is None:
        await send_menu(doctor, case)
        return {"case_id": case.case_id, "summary": "The menu was sent."}
    return await handler(case, doctor)


async def _answer_offer(command: str, doctor: DoctorProfile, msg: Optional[InboundWhatsAppMessage]) -> Dict[str, Any]:
    from app import doctor_offers
    offered = doctor_chat.offered_case(doctor)
    if offered is None:
        held = doctor_chat.doctor_case(doctor)
        await _tell(doctor, f"You are already handling {held.patient_name}'s case." if held and command == "accept"
                    else "No case is offered to you right now.")
        return {"case_id": held.case_id if held else None, "summary": "Nothing is offered to the doctor."}
    action = "ACCEPT" if command == "accept" else "DECLINE"
    await doctor_offers.handle_button(msg, doctor, [action, offered.case_id, offered.offer_id])
    return {"case_id": offered.case_id, "summary": f"The offer was {'accepted' if action == 'ACCEPT' else 'declined'}."}


async def _ask_confirmation(command: str, case: EmergencyCase, doctor: DoctorProfile) -> Dict[str, Any]:
    question = {
        "ambulance": f"Record that you called an ambulance for {case.patient_name}? They will be told on WhatsApp.",
        "close": f"Close the case with {case.patient_name}? They will be told, and you are free for new cases.",
        "handover": f"Hand {case.patient_name}'s case over? Another doctor will be found, and you are free for new cases.",
    }[command]
    title = {"ambulance": "Yes, record it", "close": "Yes, close", "handover": "Yes, hand over"}[command]
    prefix = doctor_chat.DOCTOR_PREFIX
    result = await doctor_chat._send("send_reply_buttons", doctor.phone_number, question, [
        {"id": f"{prefix}:CONFIRM:{command}:{case.case_id}", "title": title},
        {"id": f"{prefix}:CANCEL:{case.case_id}", "title": "Cancel"},
    ])
    if result.get("status") == "ERROR":
        await _tell(doctor, f"{question} Reply in the portal: {doctor_chat.portal_case_url(case.case_id)}")
    return {"case_id": case.case_id, "summary": "The doctor was asked to confirm with a button."}


async def _confirmed(command: str, case_id: str, doctor: DoctorProfile) -> Dict[str, Any]:
    """The doctor tapped a confirmation. The case must still be theirs and open."""
    from app import doctor_offers, pipeline
    case = dynamo_service.get_case(case_id)
    if case is None or case.status not in OPEN_CASE_STATUSES or case.assigned_doctor_id != doctor.doctor_id:
        await _tell(doctor, "This case is no longer yours or is already closed. Nothing was changed.")
        return {"action": f"doctor_confirm_stale", "case_id": case_id}
    if command == "ambulance":
        if case.ambulance_called_at:
            await _tell(doctor, f"Already recorded: an ambulance was called for {case.patient_name}.")
        else:
            await pipeline.mark_ambulance_called(case_id, called_by=doctor.name)
            await _tell(doctor, f"🚑 Recorded. {case.patient_name} was told an ambulance is on the way.")
        return {"action": "doctor_ambulance_called", "case_id": case_id}
    if command == "close":
        await pipeline.resolve_case(case_id, resolved_by=doctor.name)
        return {"action": "doctor_case_closed_by_doctor", "case_id": case_id}
    updated = await doctor_offers.hand_over(case_id, doctor)
    if updated is None:
        await _tell(doctor, "The case could not be handed over (it changed meanwhile). Nothing was changed.")
        return {"action": "doctor_handover_failed", "case_id": case_id}
    await _tell(doctor, f"↩️ You handed {case.patient_name}'s case over; another doctor is being found. You are free "
                        "for new cases.")
    return {"action": "doctor_handed_over", "case_id": case_id}


async def _video(case: EmergencyCase, doctor: DoctorProfile) -> Dict[str, Any]:
    from app.services.chime_service import chime_service
    session = await chime_service.create_emergency_video_session(case.case_id, doctor.name)
    updated = dynamo_service.update_case(case.case_id, {"video_room_url": session["patient_join_url"]}) or case
    await doctor_chat._send(
        "send_video_call_invitation", to_phone=case.user_phone, doctor_name=doctor.name,
        specialty=case.matched_specialty or doctor.specialty or "Emergency Medicine",
        video_url=session["patient_join_url"])
    link = chime_service.doctor_whatsapp_link(chime_service.get_case_with_video(case.case_id) or updated, doctor.doctor_id)
    await _note(case.case_id, f"📹 [Video Room Launched]: {doctor.name} started a video call from WhatsApp; the patient "
                              "was sent the 1-Click link.")
    try:
        await events_service.publish_case(dynamo_service.get_case(case.case_id) or updated)
    except Exception as e:
        print(f"[DoctorCommands] Event publish failed for {case.case_id}: {e}")
    await doctor_chat._send("send_case_link", doctor.phone_number,
                            f"📹 {case.patient_name} was sent the video call link. Tap *Join video call* to join "
                            f"(no login needed; works only for you):\n{link}",
                            "Join video call", link, header="Video call")
    return {"case_id": case.case_id, "summary": "The video call was started; the doctor was sent their link."}


async def _location(case: EmergencyCase, doctor: DoctorProfile) -> Dict[str, Any]:
    from app import pipeline
    await doctor_chat._send("send_location_request", case.user_phone, pipeline.LOCATION_REQUEST_TEXT)
    if not case.location_requested_at:
        dynamo_service.update_case(case.case_id, {"location_requested_at": doctor_chat._now().isoformat()})
    await _note(case.case_id, f"📍 {doctor.name} asked the patient for their location again (from WhatsApp).")
    text = f"📍 {case.patient_name} was asked to share their location; it comes to you here."
    if case.location_latitude is not None and case.location_longitude is not None:
        text += f" Last shared: {pipeline.maps_url(case.location_latitude, case.location_longitude)}"
    await _tell(doctor, text)
    return {"case_id": case.case_id, "summary": "The patient was asked for their location."}


async def _summary(case: EmergencyCase, doctor: DoctorProfile) -> Dict[str, Any]:
    await doctor_chat.send_case_card(case, doctor, header="Case summary", catch_up=False)
    return {"case_id": case.case_id, "summary": "The case summary was sent to the doctor."}


async def _call(case: EmergencyCase, doctor: DoctorProfile) -> Dict[str, Any]:
    from app import direct_calls
    link = await direct_calls.create_link_or_none(case, doctor)
    if not link:
        await _tell(doctor, "The call link could not be created. Please try again, or use the portal.")
        return {"case_id": case.case_id, "summary": "The call link could not be created."}
    await doctor_chat._send("send_case_link", doctor.phone_number,
                            f"📞 Tap *Open chat & call* to open {case.patient_name}'s WhatsApp chat, then tap the call "
                            f"icon. You call from your own number, so they will see it.\n\n{link}",
                            "Open chat & call", link, header="Call the patient")
    return {"case_id": case.case_id, "summary": "The doctor was sent a link to call the patient."}
