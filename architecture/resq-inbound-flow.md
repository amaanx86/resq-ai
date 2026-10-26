# ResQ-AI: inbound message flow

Diagram: `resq-inbound-flow.drawio.png` (the PNG embeds the diagram, so draw.io can open it for editing).
Source: `backend/app/worker.py`, `backend/app/pipeline.py` (`process_inbound_message`), `backend/app/registration.py`, `backend/app/doctor_offers.py`, `backend/app/doctor_chat.py`, `backend/app/doctor_verification.py`, `backend/app/admin_alerts.py`. Diagram v3 (2026-10-21): the "Registered doctor?" branch, the relay to the doctor and the waiting case with administrator alerts.

## Steps (the first match wins)
1. A message arrives through EUM Social and SNS into SQS, or locally through the Meta webhook and the API into SQS.
2. The worker long-polls the queue. On any exception it leaves the message on the queue; after 5 receives the message moves to the DLQ, which raises an alarm and emails ops.
3. Internal tasks (doctor-offer timeout, waiting-case re-check, administrator no-doctor call, doctor idle reminder) and WhatsApp call events go to `app/tasks.py`. They skip the sender lease, so they never wait behind a voice note being triaged.
4. The worker claims `msg:{provider}:{id}`. If the message is already COMPLETE, the worker deletes it. If another attempt holds the claim (IN_PROGRESS), the worker leaves the message for a retry.
5. The worker takes the `sender:{phone}` lease. A registered doctor's number is handled first (`doctor_chat.py`, DOC-1):
   - A number the doctor has not confirmed yet (DOC-8, `doctor_verification.py`) only gets the **Yes, it's me** / **No, not me** question again; a "not me" number is handled like anyone else's (steps 6 onwards).
   - **Accept** / **Decline** (`OFFER:*`) goes to `doctor_offers.py`; `DOC:*` buttons (menu rows, confirmations, **Show messages**) to `doctor_commands.py`.
   - An `@resq ...` message is a command: fixed commands without a model, anything else through the doctor agent (Bedrock), which can only run the same commands.
   - Anything else goes to the patient of the one case the doctor holds, unchanged, from the ResQ number, and into the case chat. A doctor with an offer but no case is asked to accept first; a doctor whose case closed in the last 12 hours gets "case closed"; any other doctor is handled as a patient (steps 6 onwards).
   - A patient's **Request a call** sends the assigned doctor an **Open chat & call** link (`direct_calls.py`).
   - Any other tap counts as typing the button's title.
6. The worker loads the user. An unknown sender gets a `PENDING_DETAILS` profile.
7. A user still `PENDING_DETAILS` or without an open case can register their emergency contact in their own words, typed or in a voice note. A voice note is transcribed first, once, and the transcript is reused by every later step. `registration.py` reads the message with Bedrock, or with keywords as the fallback.
   - The profile is updated and no case is created, unless the message also reports an emergency; then it is triaged too, with the contact on file.
   - A registration without a usable number gets a reply asking for it.
8. If the sender has an open case, the message goes into that case's chat for the doctor and, once a doctor took the case, to the doctor's own WhatsApp (a photo with its AI summary, a voice note with its transcript, a location as a map pin). Outside the doctor's 24-hour window it goes as the `resq_doctor_patient_waiting` template instead.
   - A shared location is saved on the case and shown to the doctor. For an ESI 1-2 case it is also sent to the emergency contact as a WhatsApp template. It is never triaged.
   - If the emergency protocol never ran on the case (human review or ESI 3), a text or voice message is re-triaged under the same case ID.
9. Greetings, unsupported message types and locations without an open case get a short reply.
10. Voice notes go to S3 and a Transcribe batch job. If transcription fails, the worker opens a human-review case (ESI 2, no contact call, location requested).
11. The Strands triage agent assigns ESI 1-5.
    - It runs on the AgentCore Runtime with Bedrock, falls back to the fallback model, then to the keyword classifier (`degraded=True`).
    - Recalled patient facts can only raise the urgency.
12. The case gets its status from the ESI level:
    - ESI 1-2: the agent runs the contact-call and doctor-ranking tools in parallel, once per case, and the case becomes `DOCTOR_HANDOVER`. No ambulance is dispatched automatically; the doctor calls it.
    - ESI 3: the case stays `ACTIVE` for a doctor to review.
    - ESI 4-5: the case is `DISMISSED`.
13. The worker persists the case and chat to DynamoDB.
14. **Doctor offer** (ESI 1-3 and human review), in `doctor_offers.py`:
    - The case goes to FREE doctors only (one case per doctor: a doctor with an open case or a pending offer is skipped; so is a doctor whose WhatsApp number is not confirmed), specialty first: the top 3 at once for ESI 1-2 (60 s per round), one for ESI 3 (5 min). Each offered doctor is locked to the case first (a conditional write on the doctor). It goes out as a WhatsApp template with Accept / Decline, and as a portal popup.
    - A delayed timeout task is queued. The first accept wins (a conditional write); a decline by the whole round or a timeout starts the next round, with the next free doctors. There is no round limit.
    - When no free doctor is left the case waits (`EXHAUSTED`, `no_doctor_since`): the patient is told to stay on the chat and to call 1122 / 115 if it gets worse; administrators see a banner with an alarm in the portal, and for ESI 1-2 their phones are called through Amazon Connect (`admin_alerts.py`, every 5 min, at most 3 times, until acknowledged). The numbers called are the administrators added in the portal with "alert calls" on, plus `ADMIN_ALERT_PHONE_NUMBERS`. The case is offered again as soon as a doctor is free (a release, a doctor coming on duty, or the 60 s re-check task); an admin can assign by hand.
15. The worker publishes portal events (through SNS to the API and its WebSockets) and sends the WhatsApp reply. ESI 1-2 cases get a **Send location** button after the reply, and new users get the onboarding prompt. Finally the worker deletes the SQS message.

## Design notes
- The case ID derives from the message idempotency key, and tool idempotency is per case, so a retry never calls the emergency contact twice.
- In mock mode (no queue), the API runs the same pipeline inline.
