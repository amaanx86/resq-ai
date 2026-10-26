# ResQ-AI: use cases

Diagram: `resq-use-cases.drawio.png` (the PNG embeds the diagram, so draw.io can open it for editing).
Sources: `backend/app/pipeline.py`, `backend/app/doctor_offers.py`, `backend/app/doctor_chat.py`, `backend/app/doctor_verification.py`, `backend/app/admin_alerts.py`, `backend/app/direct_calls.py`, `backend/app/routers/*`, `backend/app/auth.py`, `frontend/src/components/*`. Diagram v3 (2026-10-21).

## Actors
| Actor | Channel | Use cases |
|---|---|---|
| Patient | WhatsApp | Register an emergency contact (own words, typed or in a voice note); report an emergency (voice or text); receive the triage reply; share their location; message the assigned doctor (it reaches the doctor's WhatsApp); request a call from the doctor; join the video call from a link; wait for a free doctor when none is free (and call 1122 if it gets worse) |
| Emergency contact | Phone, WhatsApp | Receives an Amazon Connect call (ESI 1-2), then the patient's shared location as a WhatsApp template |
| Ops on-call | Email | Gets an alert when a message lands in the DLQ |
| Doctor | Portal (Cognito), WhatsApp | Confirm their WhatsApp number (Yes / No, before any case); sign in with the password from the invitation email; accept or decline case offers (WhatsApp buttons or the portal popup; one case at a time); monitor their own and offered cases live; chat with the patient from the portal or their own WhatsApp; run `@resq` commands on WhatsApp (video, location, summary, ambulance called, close, hand over); play the voice note and transcript; call the ambulance to the shared location and mark it called; call the patient on WhatsApp from their own number; start a video call; resolve the case; switch themselves on or off duty |
| Administrator | Portal (Cognito), phone | Everything a doctor can do, plus register, edit and unregister doctors; add, edit and remove administrators (each with a phone for the no-doctor alert calls); assign a doctor to a waiting case; get the no-doctor alert (portal banner; a Connect call for ESI 1-2) and acknowledge it; view analytics; see all cases and the audit log; delete closed cases; simulate a voice note |

## Relationships
- *Report emergency* «include» *Transcribe voice note* (voice notes only) and *Triage to ESI 1-5*. *Register emergency contact* also «include»s the transcription when it is a voice note.
- The ESI 1-2 tools (*call emergency contact*, *rank free doctors*) and *send shared location* «extend» triage, and the tools run in parallel once per case. *Offer case to free doctors* extends triage for ESI 1-3 and human-review cases; a case no free doctor takes waits for the next one.
- *Start video call* «include» *create video room*. *Request a call* «include» *send Open chat & call link to the doctor*.
- There is no ambulance dispatch use case: *share location* (patient) feeds *call ambulance* (doctor). A CAD partner integration is post-hackathon work (`docs/architecture.md` §9).
