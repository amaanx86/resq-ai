# ResQ-AI: backend architecture

Diagram: `resq-backend-architecture.drawio.png` (the PNG embeds the diagram, so draw.io can open it for editing).
Source: `backend/app`, `backend/agent_runtime`, `backend/gateway_tools`. Diagram v3 (2026-10-21).

## Deployables
| Unit | Entry point | Role |
|---|---|---|
| API service (ECS) | `app/main.py` | FastAPI routers (webhook, cases, doctors, admins, media, video, analytics, direct-call), Cognito auth (`auth.py`), WebSocket bridge, portal-event subscriber, demo seeding |
| Worker service (ECS) | `app/worker.py` | Long-polls SQS. WhatsApp messages go to `pipeline.py`; internal tasks and call events go to `tasks.py` |
| AgentCore Runtime | `agent_runtime/app.py` | Same Strands agent in an ARM64 image on port 8080; reads patient memory; tools via the Gateway |
| Gateway Lambda | `gateway_tools/handler.py` | The 3 MCP tools behind the AgentCore Gateway; reuses `agents/tools.py` |

The API and the worker ship in one image.

## Layers
1. **Orchestration.** `pipeline.py` handles idempotency, sender leases and routing (a doctor's WhatsApp, registration, handover, triage), then persistence, portal events and the WhatsApp reply.
2. **Features.**
   - `registration.py`: the emergency contact in the patient's own words.
   - `doctor_offers.py`: offer rounds (3 free doctors at once for ESI 1-2), the doctor lock (one case per doctor), accept / decline, timeout, waiting cases offered again when a doctor is free.
   - `doctor_chat.py`: doctors on WhatsApp: relay both ways, the case card, the 24-hour window, reminders. `doctor_commands.py`: the `@resq` commands and `DOC:*` buttons. `case_messages.py`: the doctor's text and media to the patient (portal and WhatsApp).
   - `doctor_verification.py`: the doctor confirms their WhatsApp number (Yes / No) before it gets a case.
   - `admin_alerts.py`: no-doctor alerts, the Connect call to the administrators for ESI 1-2.
   - `direct_calls.py`: **Request a call** and the one-time **Open chat & call** links.
   - `tasks.py`: delayed internal tasks (offer timeout, waiting-case re-check, administrator call, idle reminder) and WhatsApp call events.
3. **Agents.**
   - `triage_agent.py` and `core.py`: the Strands agent, `TriageDecision`, the local or AgentCore backend, and the keyword fallback.
   - `doctor_agent.py`: free-form `@resq` messages; its Strands tools run the fixed commands, bound to the doctor's case.
   - `tools.py`: the ESI 1-2 guard and per-case idempotency.
   - `memory.py`: AgentCore Memory recall.
   - `agentcore_client.py` and `gateway_backend.py`: the remote calls.
4. **Services.** Module singletons, each with a mock, Floci and AWS mode: `dynamo_service`, `audio_service`, `whatsapp_service` (on top of `messaging/providers.py`), `queue_service`, `events_service`, `telephony_service` (the emergency-contact and administrator calls), `chime_service`, `cognito_service` (doctor and administrator portal users), `memory_service`.
5. **AWS.** DynamoDB, S3, Transcribe, EUM Social / Meta API, SQS, SNS, Connect, Chime SDK, Cognito, Bedrock, AgentCore Memory.

## Design notes
- The singletons are created at import time, so settings must be in place before the first `app.*` import.
- `MOCK_AWS` and `AWS_ENDPOINT_URL` select mock, Floci or real-AWS mode. In mock mode the API runs the pipeline inline instead of using the queue, and internal tasks run on timers.
