# ResQ-AI: Doctor WhatsApp Integration Plan

> Design record: the plan as written before the work was built. The current behaviour is described in [`../architecture.md`](../architecture.md) and the code.

Planned 2026-10-21. Two parts:

1. **Doctor offers and no-doctor alerts (OFFER-3..5, ADMIN-1):**
   - ESI 1-2 cases go to 3 doctors at a time.
   - A doctor never has two cases (or two offers) at once.
   - A case with no doctor waits and is offered again as soon as a doctor is free.
   - Admins are alerted in the portal and by phone.
2. **Doctor WhatsApp chat (DOC-\*):** a doctor handles their case entirely from their own WhatsApp, talking to the patient through the ResQ business number.

Builds on the deployed stack (`docs/architecture.md`), the doctor offers (OFFER-1/2) and the direct calls (CALL-3) in the earlier WhatsApp features plan.

Line numbers are from 2026-10-21. Re-check them before editing.

**Status (2026-10-21):** every item below is built and tested in mock mode (697 backend tests, `npm run build`), on branch `feature/doctor-whatsapp`; nothing is deployed yet. Deploy steps: `docs/deployment.md`, Redeploys > "Deploying the doctor WhatsApp release". The real-phone "Done when" checks are in `docs/deployment.md` step 11. The architecture diagrams are updated (v3). Open: Meta's review of the `resq_doctor_patient_waiting` and `resq_doctor_confirm_number` templates.

**Added after the plan (2026-10-21, owner requests):** administrators are added in the portal with a phone number for the no-doctor calls (ADMIN-2, `app/routers/admins.py`, **Administrators** tab); a newly registered doctor confirms their WhatsApp number before any case (DOC-8, `app/doctor_verification.py`; a login over WhatsApp was considered and dropped: identity is the confirmed number, and a password typed in a chat would be stored in it); administrators edit and unregister doctors and delete closed cases; the portal follows the SUDO brand.

Decisions made while building (the open questions below, as recommended): doctors who declined a case are not asked again for it; administrator numbers come from the administrators added in the portal (ADMIN-2) plus `admin_alert_phone_numbers` in `terraform.tfvars` (all called at once); `@resq hand over` exists; doctors with no case closed in the last 12 hours are handled as patients (`@resq emergency <text>` at any time); patient messages are always forwarded to the doctor's WhatsApp; doctors' voice notes are not transcribed (P3, not built); the doctor agent runs in the worker. Patient messages stay English (as the other replies).

## Goal

Many doctors have no laptop at hand. After accepting a case on WhatsApp, the doctor should be able to:

- chat with the patient by messaging the ResQ number (text, photos, voice notes, both ways), and
- run case actions by starting a message with **`@resq`**, for example `@resq start a video call` or `@resq ambulance called`. A doctor agent reads these messages and calls the matching tools.

**One case per doctor.** A doctor handles one emergency at a time and is not offered another case while they have one. So everything a doctor sends to the ResQ number belongs to their one open case. No case tags, no "which patient?" questions.

**No case is left without someone watching it.** If no doctor accepts, the patient is told to wait, the case is offered again the moment a doctor is free, and admins are alerted (portal notification for every level, plus a phone call for ESI 1-2).

**The portal stays as it is for doctors.** Using it is optional: everything a doctor does on WhatsApp shows up in the portal chat live, and a doctor can switch between the two at any time. The only portal change is the admin alert (ADMIN-1).

## Owner decisions

| Item | Decision |
|---|---|
| Offer rounds | **ESI 1 and ESI 2 (including human-review cases): the top 3 free doctors at once, 60 s per round, then the next 3, and so on** until no free doctor who hasn't been asked is left. ESI 3: one doctor at a time, 5 minutes each, also with no round limit (2026-10-21). |
| Doctor load | **A doctor never has two cases at once, and that includes offers (2026-10-21).** While a doctor has an open case or a pending offer, they get no other offer and can't accept or be assigned another case. This applies to the whole offer system, portal included. |
| No doctor | **The patient is told to wait:** "No doctor is free right now. Stay on this chat: your case will go to a doctor as soon as one is free. If it gets worse, call 1122/115." The case is offered again automatically when a doctor is free (2026-10-21). |
| Admin alerts | **Every case without a doctor gets a portal notification for admins. ESI 1-2 cases also get an Amazon Connect call to the admin number(s):** "An ESI level 1 case has no doctor assigned. Please check the ResQ portal as soon as possible." (2026-10-21). |
| Channel | Doctors chat with the patient through the ResQ business number. Neither side sees the other's personal number. |
| Command format | **`@resq <message>`** (case-insensitive, `@resq:` also accepted). Everything else a doctor sends goes to the patient unchanged. |
| Relay | **Plain messages are relayed as they are, with no model involved.** The doctor agent only runs on `@resq` messages and never writes to the patient in its own words. |
| Portal | Unchanged for doctors and optional. The doctor's WhatsApp messages are saved to the case chat as `DOCTOR` messages. Messages sent from the portal are not copied to the doctor's own WhatsApp. |
| Direct calls | The CALL-3 direct call (**Request a call** / **Open chat & call**) stays as an option; `@resq call` gives the doctor the link. |

### Still open (needed before the item starts)

| Question | Needed by | Recommendation |
|---|---|---|
| Is a doctor who declined (or let the offer time out) asked again for the same case later, for example when they become free? | OFFER-5 | No. They said no to this case; the admin can still assign them by hand. |
| Who are the admin numbers for the call? | ADMIN-1 | One or two on-call numbers in `terraform.tfvars` (`admin_alert_phone_numbers`). All of them are called at once. |
| Can a doctor give a case back (for example they have to leave)? | DOC-4 | Yes: `@resq hand over` (with confirmation) frees the doctor and starts a new offer round for the case, without them. |
| A registered doctor messages the ResQ number with no open case. | DOC-1 | If their last case closed less than 12 h ago: reply "Your case with Ali is closed; your message was not delivered", with no triage. Otherwise treat them as a patient (normal flow), so a doctor can still report their own emergency. |
| Forward patient messages to the doctor's WhatsApp even while the doctor has the case open in the portal? | DOC-2 | Yes, always (simple, predictable). |
| Should the doctor's voice notes be transcribed for the record? | DOC-3 | Yes, in the background (Transcribe), P3. The portal shows the audio either way. |
| Where does the doctor agent run? | DOC-5 | In the worker, in-process (like `image_agent.py`). Its tools are local code (WhatsApp, Chime, DynamoDB), and AgentCore Runtime stays the triage agent. |

## Target flow

1. A case is saved that needs a doctor (ESI 1-2, human review or ESI 3).
2. We rank the FREE doctors (no open case, no pending offer), lock each one to this offer and send the offers: the top 3 with a 60 s round for ESI 1-2, the top 1 with 5 min for ESI 3.
3. If the whole round declines or times out, those doctors are released and the next 3 (or next 1) are asked, until nobody free is left.
4. The first ACCEPT wins. That doctor is locked to the case and the others in the round are released.
5. If nobody is free, or everyone declined, the case waits (status EXHAUSTED plus `no_doctor_since`). The patient is told "No doctor is free right now ... if it gets worse, call 1122/115", admins get the portal banner, alarm and browser notification at every ESI level, and for ESI 1-2 a Connect call to the admin numbers, repeated every 5 min (max 3) until someone acknowledges. The case is offered again automatically when a doctor becomes free, lowest ESI and oldest first.

Once a doctor has accepted, they get the "Case accepted" card on WhatsApp (with an optional portal link) and a catch-up of the conversation. A patient's message goes to the ResQ number, then the worker, which adds it to the case chat in the portal and forwards it to the doctor's WhatsApp as "*Ali*: ...". A doctor's message takes the same route: an "@resq ..." message goes to the commands or the doctor agent, and anything else goes to the patient of the doctor's one case as "👨‍⚕️ *Dr X*: ...".

When the doctor sends `@resq close` or `@resq hand over` (or the case is closed in the portal), the doctor is free again and is offered the next waiting case, if there is one.

## Tracker

| ID | Priority | Area | Item | Est. | Code | Still to verify |
|---|---|---|---|---|---|---|
| OFFER-3 | P1 | Offers | Offer rounds: ESI 1-2 to 3 doctors at a time, 60 s, no round limit | 0.5 d | [x] | 4 doctors: an ESI 2 case goes to 3, then to the 4th |
| OFFER-4 | P1 | Offers | One case per doctor, offers included: doctor lock, busy doctors skipped, `BUSY` on accept / assign | 1 d | [x] | Two cases at the same moment never reach the same doctor |
| OFFER-5 | P1 | Offers | Waiting cases: patient wait message, offered again when a doctor is free, re-check task | 1 d | [x] | Waiting case is offered when a doctor closes their case |
| ADMIN-1 | P1 | Alerts | No-doctor alerts: admin banner + alarm + browser notification, Connect call for ESI 1-2, acknowledge | 1.5 d | [x] | Admin's real phone rings; Acknowledge stops the repeat calls |
| DOC-0 | P1 | WhatsApp | Groundwork: doctor 24-hour window tracking, location send | 0.5 d | [x] | - |
| DOC-1 | P1 | Pipeline | Doctor routing: a doctor's message goes to their one open case | 0.5 d | [x] | Doctor text no longer opens a patient case |
| DOC-2 | P1 | Pipeline | Text relay both ways, case card and catch-up on accept | 1 d | [x] | Real doctor and patient phones chat end to end |
| DOC-3 | P1 | Media | Photo, voice note and location relay both ways | 1 d | [x] | Voice note and photo arrive on both real phones |
| DOC-4 | P1 | Commands | `@resq` commands: menu, video (doctor link token), location, ambulance, summary, close, hand over, call, accept/decline | 1 d | [x] | Video call joined from the doctor's phone browser via WhatsApp link |
| DOC-5 | P2 | Agent | Doctor agent for free-form `@resq` messages (Strands, Bedrock) | 1 d | [x] | Urdu/English free-form commands on Bedrock |
| DOC-6 | P2 | WhatsApp | Template for patient messages that arrive outside the doctor's 24-hour window | 0.5 d | [x] (template awaits Meta) | Template APPROVED; doctor silent >24 h gets it and the reply works |
| DOC-7 | P2 | Docs | `docs/architecture.md` §10, the earlier WhatsApp features plan, `docs/deployment.md` step 11, inbound-flow diagram, demo script | 0.5 d | [~] diagram PNG open | - |

Total about 10 days. Recommended order: OFFER-3, OFFER-4, OFFER-5, ADMIN-1, then DOC-0, DOC-1, DOC-2, DOC-6 (submit the template early, Meta approval is outside our control), DOC-3, DOC-4, DOC-5, DOC-7.

- OFFER-3..5 and ADMIN-1 stand on their own; they improve the portal flow even before any DOC item.
- DOC-1 + DOC-2 on top of them are already a usable WhatsApp feature.

---

## OFFER-3: Offer rounds

**Problem.**
- `offer_fanout` (`backend/app/doctor_offers.py:61`) fans out only for ESI 1. ESI 2 goes to one doctor per round.
- Timeouts are 45 / 60 / 300 s (`backend/app/config.py:103-109`).
- `next_round` gives up after `DOCTOR_OFFER_MAX_ROUNDS` = 3 rounds (`doctor_offers.py:164`), even when free doctors are left.

**Design.**
- **ESI 1 and ESI 2:** the top 3 free doctors per round, 60 s per round. This includes human-review cases, which are ESI 2.
- **ESI 3:** one doctor per round, 300 s (unchanged).
- **No round limit.** Rounds continue until `rank_candidates` returns nobody. Doctors who declined or timed out are excluded for the case, as today (`declined_doctor_ids` + `also_exclude`). Then the case waits (OFFER-5).
- **Escalation.** When a follow-up escalates an ESI 3 case to ESI 1-2, the pending round restarts with the urgent fan-out and timeout (`start_offer(restart=True)`, `pipeline.py:743`).
- **Settings:**
  - `DOCTOR_OFFER_FANOUT_ESI1` becomes `DOCTOR_OFFER_FANOUT_URGENT` (ESI ≤ 2, default 3).
  - `DOCTOR_OFFER_TIMEOUT_ESI1` default 60 (same as ESI 2).
  - `DOCTOR_OFFER_MAX_ROUNDS` is removed.
  - Terraform: `doctor_offer_fanout_esi1` becomes `doctor_offer_fanout_urgent`, `doctor_offer_timeout_seconds.esi1 = 60`, `doctor_offer_max_rounds` is removed (`terraform/variables.tf:564-584`, `terraform/ecs.tf:48-54`).

**Changes.** `doctor_offers.py` (`offer_fanout`, `next_round`), `config.py`, `variables.tf`, `ecs.tf`.

**Tests.**
- Update `backend/tests/test_doctor_offers.py`. It assumes ESI 2 goes to one doctor in :64, :108, :122, :140, :200, :217, :258 and :271. The "45 seconds" check at :302 becomes "60 seconds". The max-rounds patch at :140 is removed.
- Update `backend/tests/test_location_flow.py:66` (patient text, OFFER-5).
- New tests:
  - ESI 2 fans out to 3.
  - The fourth round still runs when doctors are left.
  - Escalation restarts with a fan-out of 3.
- `offers.case()` defaults to ESI 2. Seeded demo doctors join the General Emergency and any-active tiers, so fan-out tests must count them or turn seeding off.

**Done when.** With 4 doctors on duty, an ESI 2 case goes to 3 of them at once. After 60 s without an answer it goes to the 4th, and then it waits.

---

## OFFER-4: One case per doctor, offers included

**Problem.**
- Nothing in the code stops a doctor from holding several cases.
- `rank_candidates` (`backend/app/agents/tools.py:130`) puts the least busy doctors first (`open_case_loads`, `backend/app/services/dynamo_service.py:393`) but never skips a busy one.
- `accept_offer` (`doctor_offers.py:241`) only checks the case, and `assign_by_admin` (`doctor_offers.py:353`) checks nothing.
- Two cases ranked at the same moment (two worker messages from different patients) can pick the same free doctor.

**Design.**
- **Doctor lock.** New fields on the doctor item: `busy_case_id`, `busy_kind` (`OFFER` | `CASE`), `busy_until`.
  - `dynamo_service.claim_doctor(doctor_id, case_id, kind, until)` is a conditional write. It succeeds when the doctor is free, their `OFFER` lock has expired, or the lock is already for this case.
  - `release_doctor(doctor_id, case_id)` only clears the lock if it is for that case.
  - Both have a DynamoDB branch and an in-memory branch.
- **Offer.** `next_round` ranks, then claims each chosen doctor (`OFFER`, until the round expires + 30 s). Only the doctors it could claim get the offer. If none could be claimed, it ranks again (a few tries), and after that the case waits (OFFER-5).
- **Ranking.** `rank_candidates` skips busy doctors: those with a valid lock, and, as a safety net, anyone `open_case_loads` counts as assigned to an open case.
- **Accept.** `accept_offer` turns the doctor's `OFFER` lock into a `CASE` lock (condition: same case), then does the existing conditional case write. If the case write loses (another doctor won), the doctor is released. The other doctors in the round are released and told "accepted by another doctor" (`_tell_taken`).
- **Release.** The lock is cleared when:
  - the doctor declines (`decline_offer`), or the round times out (`handle_timeout`) or is superseded;
  - the case is resolved (`pipeline.resolve_case`, portal or `@resq close`), handed over (`@resq hand over`) or reassigned by an admin.

  Each release runs `offer_waiting_cases()` (OFFER-5).
- **Self-healing.** A missed release can't block a doctor forever:
  - an `OFFER` lock past `busy_until` counts as free;
  - a `CASE` lock whose case is closed or no longer assigned to the doctor counts as free when ranking. The next claim overwrites it.
- **Busy answers.**
  - **Accept while busy:** new result `BUSY` ("You are already handling a case with Ali. Close or hand it over first."). It goes back as the WhatsApp reply to the Accept button, and as a 409 from `POST /api/v1/cases/{id}/offer/accept` (the portal already shows error texts).
  - **Admin assign to a busy doctor:** 409 "Dr X is busy with case ...". Assigning a case that already had a doctor releases that doctor.
- **Existing data.** Doctors with several open cases today (demo seeds, old cases) have no lock. Ranking treats them as busy through the `open_case_loads` safety net until those cases close. The demo seeds are changed to give each demo doctor at most one open case.

**Changes.** `schemas.py` (`DoctorProfile` fields), `dynamo_service.py`, `agents/tools.py` (`rank_candidates`), `doctor_offers.py` (round, accept, decline, timeout, admin assign), `pipeline.resolve_case`, `routers/cases.py` (409 for `BUSY`), `analytics.seed_demo_cases` / `main._seed_mock_data`.

**Tests.**
- A busy doctor is never ranked.
- Two rounds claiming the same doctor at once: only one offer is sent.
- Accept while busy returns `BUSY` / 409.
- The losing doctors of a round are released.
- Expired `OFFER` and stale `CASE` locks count as free.
- Admin assign to a busy doctor returns 409.
- The existing OFFER-1 tests stay green.

**Done when.** With 3 doctors on duty, two ESI 1 cases arrive within a second. Each doctor gets at most one offer, and nobody ever holds two cases.

---

## OFFER-5: Waiting cases

**Problem.**
- When nobody accepts, `_exhaust` (`doctor_offers.py:210`) tells the patient to call 1122/115 for an ambulance now. The same text is in `pipeline.py:1063` (`_reply_for`) and `:958` (`_human_review_reply`).
- Nothing ever offers the case again; only an admin can assign it.
- `start_offer` falls through to `next_round` for an EXHAUSTED case (`doctor_offers.py:150`), so a retry or an escalation re-runs `_exhaust`.

**Design.**
- **Waiting state.** The status stays `EXHAUSTED`, so the existing "No doctor accepted" badge and the admin assign UI (`frontend/src/components/offers.jsx:57`, `EmergencyConsole.jsx` `DoctorOfferCard`) keep working. New case fields:
  - `no_doctor_since` (when this waiting period started);
  - `no_doctor_reason`: `NO_DOCTOR_FREE` (nobody on duty or everyone busy) or `ALL_DECLINED` (every free doctor declined or timed out).
- **Patient text** (`_exhaust`, `_reply_for`, `_human_review_reply`), in the patient's language like the other replies:
  - ESI 1-2: "⏳ No doctor is free right now. Stay on this chat: your case will go to a doctor as soon as one is free. If it gets worse, call 1122/115."
  - ESI 3: the same, ending "If it gets worse, go to the nearest emergency department or call 1122/115."
  - Sent once per waiting period. The first reply already says it when nobody was free at triage time.
- **Offered again.** `offer_waiting_cases()` takes the waiting cases (open, `EXHAUSTED`) and starts a new round for each while free doctors are left: lowest ESI first, then oldest `no_doctor_since`. It runs when a doctor becomes free:
  - an OFFER-4 release;
  - a doctor switching on duty (`DutyToggle`, which calls `dynamo_service.update_doctor_status`);
  - a new active doctor registering.

  Doctors who declined that case are not asked again (open question).
- **Safety net.** Internal task `waiting_case_recheck` (`app/tasks.py` `@task_handler`), queued by `_exhaust` with a 60 s delay. It re-checks the case and re-queues itself while the case is still waiting. It does nothing once the case has a doctor or is closed, so it is idempotent.
- **`start_offer`** gets an explicit `EXHAUSTED` branch: start a new round only if a free doctor who hasn't declined exists, otherwise do nothing (no second patient message).
- When a doctor accepts a waiting case, the patient gets the normal "doctor accepted" message. `no_doctor_since` stays on the case for analytics (time waiting).

**Changes.** `doctor_offers.py` (`_exhaust`, `start_offer`, `offer_waiting_cases`, `waiting_case_recheck`), `pipeline.py` (reply texts), `dynamo_service.update_doctor_status` / doctor registration (call `offer_waiting_cases`), `tasks.py` (`HANDLER_MODULES` unchanged: handlers stay in `app.doctor_offers`), `schemas.py`.

**Tests.**
- The wait message is sent once, in the patient's language.
- A waiting case is offered when a doctor's case closes, when a doctor comes on duty, and by the re-check task.
- Lowest ESI goes first.
- Decliners are not asked again.
- A retry of an `EXHAUSTED` case sends no second message.
- `test_location_flow.py:66` gets the new text.

**Done when.** With every doctor busy, a new ESI 1 case's patient gets the wait message. When one doctor closes their case, the waiting case is offered to them within seconds.

---

## ADMIN-1: No-doctor alerts

**Problem.**
- When a case has no doctor, the only signal is a chat note and a red badge in the portal.
- No admin is notified, and nobody is called.
- No admin phone number is stored anywhere.

**Design.**

**Portal (admins only, every ESI level).**
- Worked out from case state, like the doctors' offer popup (`frontend/src/App.jsx:148-158`). Admins already receive every `case_upserted` event, so no new event type is needed.
- New `AdminAlerts` component in `App.jsx`, shown to `isAdmin` users on every tab. It is a red banner listing each open case with `doctor_offer_status == "EXHAUSTED"`:
  - ESI, patient name, reason ("No doctor free" / "All doctors declined"), minutes waiting;
  - **Open case** (opens the case, where `DoctorOfferCard` already has the assign UI);
  - **Acknowledge**.
- **Alarm.** A sound repeats every 30 s until someone acknowledges. It extends `playOfferAlert` (`offers.jsx:64`) with a more urgent pattern for ESI 1-2.
- **Browser notification.** The Notification API shows a system notification ("ESI 1 case has no doctor, Ali"), so the admin sees it even when the portal tab is in the background. A one-time "Enable alerts" button asks for permission. This works while the portal is open in any tab. Real Web Push (portal closed) needs the PWA work, which is on hold; the phone call covers that case.
- An `offer_id` / `no_doctor_since` set in a `useRef` stops the same waiting period from notifying twice.

**Acknowledge.**
- `POST /api/v1/cases/{id}/no-doctor-alert/ack` (Administrators only) sets `admin_alert_ack_by` / `admin_alert_ack_at` for the current waiting period and adds a chat note.
- Other admins see "Acknowledged by ...", the alarm stops for everyone, and the repeat calls stop.

**Phone call (ESI 1-2 only).**
- `_exhaust` queues the internal task `admin_no_doctor_call` (`queue_service.publish_internal`). It runs in the worker, which already has `connect:StartOutboundVoiceContact` (`terraform/iam.tf:300`). The API task role has none and doesn't need it; `_exhaust` can run in the API when a doctor declines in the portal.
- The handler stops if the case has a doctor, is closed, or was acknowledged. Otherwise it calls every number in `ADMIN_ALERT_PHONE_NUMBERS` at once.
- The call goes through a new `telephony_service.place_announcement_call(phone, spoken_message, language="English")`:
  - It is the same `start_outbound_voice_contact` call as `call_emergency_contact` (`backend/app/services/telephony_service.py:45`), with the same simulated mode and the same FAILED result handling.
  - It passes only `Language` and `SpokenMessage`.
  - **No new contact flow:** the existing flow (`terraform/telephony_connect.tf:24-93`) reads just those two attributes, uses Joanna for English and plays the message twice.
- **Spoken message:** "This is ResQ AI with an urgent alert. An ESI level 1 case has no doctor assigned. No doctor is available. Please check the ResQ portal as soon as possible." The level comes from the case. "No doctor is available" becomes "All doctors declined" for `ALL_DECLINED`.
- **Repeat.** The task re-queues itself every `ADMIN_ALERT_REPEAT_SECONDS` (300) while the case still waits and nobody has acknowledged it, up to `ADMIN_ALERT_MAX_CALLS` (3) calls per waiting period.
- **Idempotency.** Each call runs in `run_once(case_id, f"admin_call:{no_doctor_since}:{n}")` (`backend/app/agents/tools.py:61`). An SQS retry can't call twice, and a new waiting period (the case waits again after a hand-over) can call again.
- **Record.** Each result (number, contact ID, status, error) is appended to `admin_alert_calls` on the case, with a chat note ("📞 Admin alert call to +92... placed" / "failed: ...").
- Mock and Floci modes simulate the call, as for the emergency contact.

**Config.**
- `ADMIN_ALERT_PHONE_NUMBERS` (comma-separated E.164), `ADMIN_ALERT_REPEAT_SECONDS`, `ADMIN_ALERT_MAX_CALLS`.
- Terraform variables `admin_alert_phone_numbers` (`list(string)`, default `[]`), `admin_alert_repeat_seconds`, `admin_alert_max_calls`. They go into `local.common_app_config` (`terraform/ecs.tf:17-55`) and `terraform.tfvars.example`.
- With no numbers set, only the portal alert runs, and the worker logs a warning at startup.
- The Connect number must be able to call the admin's country. Pakistani and Indian numbers work (the emergency contact already calls them).

**Changes.**
- Backend: `doctor_offers.py`, `telephony_service.py`, `routers/cases.py` (ack endpoint), `schemas.py`, `config.py`.
- Frontend: `App.jsx`, new `components/AdminAlerts.jsx`, `offers.jsx` (alarm), `services/api.js` (ack).
- Terraform: `variables.tf`, `ecs.tf`, `terraform.tfvars.example`.

**Tests.**
- An ESI 1-2 wait queues the call task; ESI 3 does not.
- The handler calls every number and records the results.
- The handler stops after an ack, an accept or a close, and after the maximum number of calls.
- SQS retry idempotency.
- A FAILED call is noted.
- The ack endpoint is admin-only.
- No numbers set: no call, no error.

**Done when.**
- With no doctor on duty, an ESI 1 case shows the banner, alarm and browser notification to an admin, and the admin's real phone rings with the English message.
- A second call comes 5 minutes later.
- Acknowledge in the portal stops further calls.

---

## DOC-0: Groundwork

**Problem.** WhatsApp only allows free-form messages to a doctor within 24 h of the doctor's last message, and there is no way to send a location message yet.

**Design.**
- `DoctorProfile.last_inbound_at`, updated on every inbound message from a doctor's number. Button taps count, so tapping Accept opens the window. `doctor_window_open(doctor)` means the last message was less than 23 h ago (1 h margin).
- Provider `send_location(to, lat, lng, name, address)` (WhatsApp `location` message) in `app/messaging/providers.py` (all three providers) and `whatsapp_service`, for forwarding the patient's location in DOC-3.

**Tests.** `doctor_window_open` edges; location payload for each provider.

---

## DOC-1: Doctor routing

**Problem.** `_route` (`app/pipeline.py:340`) only treats `OFFER:*` button taps from a doctor as doctor actions. Any other message from a doctor's number creates a `PENDING_DETAILS` patient profile and is triaged as a new emergency.

**Design.** New module `app/doctor_chat.py`, entered from `_route` before the patient path:

1. `doctor = dynamo_service.get_doctor_by_phone(phone)`; not a doctor: patient flow as today.
2. Update `last_inbound_at` (DOC-0).
3. Button tap with a doctor prefix: `DOCTOR_ACTIONS` (as today, plus the new `DOC` prefix from DOC-4).
4. Text starting with `@resq`: `doctor_commands.handle` (DOC-4/5).
5. Otherwise the message is for the doctor's case: their `CASE` lock, `busy_case_id` (OFFER-4). This is re-checked on every message: the case is open (`OPEN_CASE_STATUSES`) and still assigned to this doctor, and the doctor `is_active`. Anything else: short reply, nothing relayed.
6. No case but an offer pending for them (`OFFER` lock): "Accept the case first" + Accept/Decline buttons for that round.
7. No case at all: the owner decision above (closed-recently reply, otherwise patient flow).

Runs under the existing per-sender lease, so a doctor's messages are handled in order.

**Changes.** `pipeline.py` (`_route` branch, `DOCTOR_ACTIONS` gains `"DOC"`), new `app/doctor_chat.py`.

**Tests.**
- Doctor text no longer creates a user profile or triage.
- The relay goes to the doctor's case only.
- A reassigned or closed case is refused.
- Offered but not yet accepted: the "Accept the case first" reply.
- A doctor without cases falls back as decided.

**Done when.** A real doctor's messages to the ResQ number reach their patient and never open a case of their own while they are on one.

---

## DOC-2: Text relay both ways

**Design.**
- **Doctor to patient.**
  - Move the body of `send_doctor_message` (`app/routers/cases.py:51`) into a shared `app/case_messages.py: send_doctor_text(case, doctor_name, text, source)`. The portal endpoint calls it, so its behaviour stays identical.
  - The WhatsApp path calls the same function. The patient gets `👨‍⚕️ *Dr X*: text`. The case chat gets a `DOCTOR` message with a stable ID from the inbound message and its `provider_message_id`, so an SQS retry doesn't double-send. Patient memory gets the doctor's words (MEM-1).
- **Patient to doctor.**
  - In `_save_patient_followup` (`app/pipeline.py:499`), after saving, when the case has an assigned doctor: `forward_to_doctor(case, chat_message)` sends `*Ali*: <text>` to the doctor.
  - This covers handover messages and follow-ups that are re-triaged. If a follow-up escalates the ESI, the doctor also gets "⚠️ Escalated to ESI 1".
- **Accept / admin assign.**
  - The doctor's "Case accepted" message (`doctor_offers.accept_offer`, `app/doctor_offers.py:281`) becomes a case card: patient, ESI, specialty, clinical summary, image analysis, `patient_history`, location if shared, "Reply here to talk to the patient", `@resq help`, and the portal link (optional).
  - Then a catch-up of the patient messages since the case opened (last 5, newest last).
  - The patient's "doctor accepted" text already says "Anything you send here goes straight to the doctor"; keep it.
- **Case closed** (portal or `@resq close`): tell the doctor the case is closed and they are free (OFFER-4 release).
- **Doctor window closed** (DOC-0): don't send free-form; DOC-6 sends the template instead. Until DOC-6 ships: try the send, log the failure, note it in the case chat.

**Tests.**
- Text relayed both ways in mock mode.
- Chat messages saved with the right sender.
- The portal endpoint is unchanged (existing tests stay green).
- Retry idempotency.
- No forwarding before acceptance.
- Closed-case notice.

**Done when.** A doctor and a patient on real phones exchange several messages through the ResQ number, and the portal chat shows all of them live.

---

## DOC-3: Photos, voice notes and location

**Design.**
- **Doctor to patient.** A doctor's `image` / `audio` message:
  - `fetch_media_to_s3` into `outbound/<case>/`.
  - Then the same steps as the portal's `send_doctor_media` (`app/routers/cases.py:77`), moved into `case_messages.send_doctor_media(...)`:
    - `media_convert.prepare_image` / `prepare_audio`. WhatsApp voice notes are already OGG/Opus; keep the conversion as a safety net.
    - `send_media_message`.
    - A `DOCTOR` chat message with `image_s3_key` / `audio_s3_key`.
  - A photo's caption goes along.
  - Optional: a background Transcribe of the doctor's voice note into the chat line (open question above).
- **Patient to doctor.**
  - Photo: re-send the stored image (`images/`) to the doctor with the caption `*Ali*: <caption>: AI summary: ...`.
  - Voice note: re-send the audio plus a text with the transcript.
  - Location: `send_location` to the doctor, plus the Google Maps link line already built in `_location_shared` (`app/pipeline.py:567`).
- Doctor sends a document, sticker, contact, etc.: "Only text, photos and voice notes reach the patient."

**Tests.** Media relayed both ways with the mock provider; a conversion failure replies to the doctor without saving anything; a location is forwarded once.

**Done when.** On real phones:
- the patient's photo and voice note reach the doctor;
- the doctor's photo and voice note reach the patient;
- the patient's shared location opens as a map pin on the doctor's phone.

---

## DOC-4: `@resq` commands

**Design.** `app/doctor_commands.py`. A message starting with `@resq` is never relayed to the patient, and every command acts on the doctor's one case. The rest of the text is matched against fixed commands first (English plus a few Urdu/Roman-Urdu words), with no model needed:

| Command (examples) | Does | Reuses |
|---|---|---|
| `@resq` / `@resq help` / `@resq menu` | Interactive list of the commands below | `send_reply_buttons` / new list send |
| `@resq video` | Creates or reuses the meeting, sends the patient the invite, sends the doctor **their own join link** | `chime_service.create_emergency_video_session`, `send_video_call_invitation`, as in `cases.py:252` |
| `@resq location` | Sends the patient the "Send location" button again | `pipeline._request_location` |
| `@resq ambulance called` | Asks to confirm, then records it and tells the patient | `pipeline.mark_ambulance_called` |
| `@resq summary` | Case card again (triage, history, photo analysis, location) | DOC-2 case card |
| `@resq close` | Asks to confirm, then resolves the case and frees the doctor | `pipeline.resolve_case`, OFFER-4 release |
| `@resq hand over` | Asks to confirm, then frees the doctor, tells the patient another doctor is being found, and starts a new offer round without them | `doctor_offers.start_offer(restart=True)`, OFFER-4 release |
| `@resq call` | The **Open chat & call** link (the patient will see the doctor's number) | `direct_calls.create_link` |
| `@resq accept` / `@resq decline` | Answers the offer pending for them | `doctor_offers.accept_offer` / `decline_offer` |

- **Confirmations** for ambulance, close and hand over:
  - Reply buttons with payload `DOC:CONFIRM:<action>:<case>` / `DOC:CANCEL`, handled through `DOCTOR_ACTIONS["DOC"]`. The assignment is re-checked when the button is tapped.
  - Every command is recorded in the case chat as a `BOT` line ("📹 Dr X started a video call from WhatsApp"), so the portal shows it.
- **Doctor video link.**
  - The doctor's join link needs a portal login today (`app/routers/video.py:42`, `auth.portal_auth`).
  - Add `video_doctor_token` on the case, like `video_patient_token`: created with the meeting, rotated when the case is reassigned or handed over.
  - `/call/<meeting>?role=doctor&case=..&token=..` is accepted without a portal login when the token matches and the doctor is still assigned to the open case.
  - The portal's existing doctor link (no token, portal login) keeps working unchanged. `chime_service.build_join_urls` (`app/services/chime_service.py:83`) gains the token variant for WhatsApp.
- Unknown command: handed to the doctor agent (DOC-5). Before DOC-5 ships, reply with the menu.

**Tests.**
- Each command in mock mode.
- The confirmation flow, and stale confirmations (case reassigned or closed).
- Hand over starts a new round without the doctor.
- The doctor token is accepted only for the assigned doctor and an open case.
- The portal video path is unchanged.

**Done when.** On a real phone:
- a doctor runs `@resq video`, and both join from the WhatsApp links (the doctor without logging in to the portal);
- `@resq ambulance called` and `@resq close` work with confirmation.

---

## DOC-5: Doctor agent for free-form `@resq` messages

**Problem.** Doctors will write things like `@resq patient ko video call bhejo` or `@resq I've called 1122`. Fixed commands won't cover them.

**Design.** `app/agents/doctor_agent.py`, a Strands agent on Bedrock, run in the worker. Model: `DOCTOR_AGENT_MODEL_ID` (default `BEDROCK_MODEL_ID`), then `BEDROCK_FALLBACK_MODEL_ID`.
- **Tools** are the DOC-4 command functions: `start_video_call`, `request_location`, `mark_ambulance_called`, `get_case_summary`, `close_case`, `hand_over`, `direct_call_link`, `answer_offer`.
  - The doctor ID and their case ID are bound in code, not taken from the model, as the triage tools do with the case ID (`app/agents/tools.py`).
  - Ambulance, close and hand over still only produce the confirmation buttons; the doctor's tap does the change.
- **Context:** the doctor's `@resq` text, the doctor's name, and their case as structured fields (patient name, ESI, specialty, status).
  - **No patient free text** goes into the prompt, so a patient cannot steer the agent ("close my case").
  - Tool results that contain patient words (the summary) are sent to the doctor by code, not fed back to the model.
- **Output:** a short reply to the doctor only. The agent has no tool that sends free text to the patient. If it can't tell what the doctor wants, it asks or shows the menu; it never guesses an action.
- **Limits:** one agent run per message, at most 3 tool calls, 20 s timeout: "Sorry, I couldn't do that. @resq help".
- **Mock/Floci mode:** the DOC-4 keyword parser only (like `heuristic_clinical_eval`), so tests need no Bedrock.

**Tests.** The mock parser handles the examples above; tools refuse a case that isn't the doctor's; the prompt contains no patient text; the failure fallback works.

**Done when.** On Bedrock, a set of 10 English / Urdu / Roman-Urdu free-form commands each runs the right tool or asks a clear question.

---

## DOC-6: Patient messages outside the doctor's 24-hour window

**Problem.** WhatsApp only allows free-form messages to the doctor within 24 h of the doctor's last message. A long case, or a doctor who accepted in the portal and never messaged the ResQ number, would silently miss patient messages.

**Design.**
- New utility template `resq_doctor_patient_waiting` in `terraform/whatsapp_templates.tf`: "Your patient {{1}} sent you a new message on ResQ-AI." It has a quick-reply button **Show messages** (payload `DOC:SHOW:<case>`).
- Tapping the button opens the window, and the handler sends the messages the doctor missed.
- `forward_to_doctor` sends the template instead of the message when `doctor_window_open` is false, at most once per 30 minutes.
- Setting: `WHATSAPP_DOCTOR_WAITING_TEMPLATE_NAME`.

**Done when.** The template is APPROVED. A doctor who has been silent for over 24 hours receives it, taps the button and gets the patient's messages.

---

## DOC-7: Docs and diagrams

- `docs/architecture.md` §10: the new owner decisions.
- the earlier WhatsApp features plan: OFFER-1 note on ESI 2 fan-out and the round limit, plus a pointer to this file.
- `docs/deployment.md` step 11: the "Done when" checks above. Demo script: a doctor works the case from a phone; no doctor on duty: the admin gets the alert call.
- `architecture/` inbound-flow diagram: the doctor branch and the waiting-case path, with its PNG re-exported.

---

## Settings and infrastructure

| Setting | Default | Notes |
|---|---|---|
| `DOCTOR_OFFER_FANOUT_URGENT` | `3` | OFFER-3. Replaces `DOCTOR_OFFER_FANOUT_ESI1`; applies to ESI 1-2. Terraform `doctor_offer_fanout_urgent`. |
| `DOCTOR_OFFER_TIMEOUT_ESI1` / `_ESI2` / `_ESI3` | `60` / `60` / `300` | OFFER-3. ESI 1 was 45. |
| `DOCTOR_OFFER_MAX_ROUNDS` | removed | OFFER-3: rounds continue until no free doctor is left. |
| `WAITING_CASE_RECHECK_SECONDS` | `60` | OFFER-5. |
| `ADMIN_ALERT_PHONE_NUMBERS` | empty | ADMIN-1. Comma-separated E.164; Terraform `admin_alert_phone_numbers` (`list(string)`). Empty: portal alert only. |
| `ADMIN_ALERT_REPEAT_SECONDS` / `ADMIN_ALERT_MAX_CALLS` | `300` / `3` | ADMIN-1. Per waiting period, until acknowledged. |
| `DOCTOR_WHATSAPP_RELAY_ENABLED` | `True` | Off: doctor text gets a "use the portal" reply instead of being relayed (DOC-1 routing stays on, so it is never triaged). Terraform variable on the worker. |
| `DOCTOR_AGENT_MODEL_ID` | `BEDROCK_MODEL_ID` | DOC-5. |
| `DOCTOR_CASE_CLOSED_GRACE_HOURS` | `12` | DOC-1 open question. |
| `WHATSAPP_DOCTOR_WAITING_TEMPLATE_NAME` | `resq_doctor_patient_waiting` | DOC-6. |

No new AWS resources:
- The worker already has DynamoDB, S3, Bedrock, Chime, Connect and WhatsApp permissions.
- The admin call reuses the existing Connect instance, flow and number.
- New settings go through `local.common_app_config` (`terraform/ecs.tf`) into the config secrets, and `terraform apply` rolls the services.

## Risks

| Risk | Mitigation |
|---|---|
| Fewer doctors are available: with one case each and 3 doctors held by every ESI 1-2 round, a busy shift runs out of free doctors and more cases wait. | The patient is told to wait, with the 1122/115 line if it gets worse. Waiting cases are offered the moment a doctor is free, lowest ESI first. Admins are alerted. Have enough doctors on duty for the demo. |
| ESI 1-2 rounds hold 3 doctors for up to 60 s, so a second urgent case may find nobody free for a minute. | Released doctors immediately trigger `offer_waiting_cases`; the 60 s re-check is the safety net. |
| A missed release leaves a doctor locked. | `OFFER` locks expire with the round. `CASE` locks are checked against the case when ranking. |
| A doctor forgets to close a finished case and blocks themselves (and new offers). | The case card and `@resq help` say how to close. A reminder after 2 hours without messages ("Still treating Ali? `@resq close` when done"). Admins can close cases in the portal. |
| An ESI 3 case keeps a doctor busy while an ESI 1 waits. | Waiting cases are ordered by ESI; the doctor can `@resq hand over`. A priority override can be considered later. |
| Admins get too many calls (call fatigue) or miss them. | Calls only for ESI 1-2, at most 3 per waiting period, stopped by Acknowledge. The portal banner and notification stay until the case has a doctor. |
| The admin call fails (Connect quota, wrong number). | FAILED calls are noted in the case chat. The portal alert doesn't depend on the call. |
| The model changes a doctor's medical instruction. | Plain messages never go through a model; the agent has no "send text to patient" tool. |
| A patient steers the agent through their messages (prompt injection). | No patient text in the agent's prompt; irreversible actions need the doctor's button tap. |
| A doctor's number is also a patient's number. | Owner decision for doctors with no open case (patient flow after the grace period). |
| Doctor misses patient messages outside the 24-hour window. | DOC-6 template. |
| WhatsApp messaging limits and per-message cost go up (every patient message is sent twice). | Service messages inside an open window are free; watch the number's quality rating in the Meta dashboard. |
| A doctor's lost or shared phone. | Same trust as today's Accept button (the WhatsApp number is the identity); deactivating the doctor stops routing immediately. |

## Relation to other documents

- the earlier WhatsApp features plan:
  - OFFER-1/2: the accept flow this builds on. OFFER-3..5 here change its fan-out, its round limit and what happens when nobody accepts.
  - CALL-3: direct calls, kept as `@resq call`.
- `docs/architecture.md` §10: the earlier owner decisions still hold. Doctors accept before assignment, and there's no automatic ambulance dispatch (`@resq ambulance called` only records the doctor's own call). New decisions: the round rule, one case per doctor, waiting cases and admin alerts.
- `docs/deployment.md` step 11: add the "Done when" checks above once the items are built.
