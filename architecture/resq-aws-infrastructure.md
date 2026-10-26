# ResQ-AI: AWS infrastructure

Diagrams: `resq-aws-infrastructure-overview.drawio.png` (**Overview**) and `resq-aws-infrastructure-network.drawio.png` (**Network**). The PNGs embed the diagram, so draw.io can open them for editing.
Sources: `terraform/*.tf` (deployed in `us-east-1` with `terraform apply`, remote state in `terraform/backend.hcl`), `terraform/terraform.tfvars` for this deployment's switches, `backend/app`, `.gitlab-ci.yml`.

## Flow (Overview page)
1. **Portal.** Doctors and admins sign in through the Cognito Hosted UI (PKCE). CloudFront serves the React SPA from the S3 portal bucket through OAC, and proxies `/api/*` and `/ws/*` to the ALB. When an admin registers a doctor, the API creates the doctor's Cognito user and Cognito emails the invitation.
2. The ALB is in the public subnets and accepts only CloudFront origin-facing IPs that carry the `X-Origin-Verify` header. It forwards to the API tasks (ECS Fargate, port 8000) in the private subnets.
3. **WhatsApp.** The patient's message (or a doctor's, from their own WhatsApp) reaches AWS End User Messaging Social. EUM Social publishes it to SNS `resq-whatsapp-inbound-events`, which delivers it to SQS `resq-webhook-queue`. Everything the worker sends (replies, location requests, doctor case offers, templates) goes back out through EUM Social.
4. The worker (ECS Fargate, scaled on queue depth) long-polls the queue. It stores voice notes in the S3 audio vault and transcribes them with a Transcribe batch job. It also puts delayed internal tasks (doctor-offer timeouts, waiting-case re-checks, administrator calls, doctor reminders) on the same queue.
5. The worker calls `InvokeAgentRuntime`. The Strands triage agent on the AgentCore Runtime classifies the case through the Bedrock Converse API (primary model, then the fallback model), and recalls the patient's facts from AgentCore Memory. The worker and the API write patient events to that memory.
6. **ESI 1-2 only.** The runtime calls the AgentCore Gateway over MCP with IAM auth. The Gateway's Lambda target (3 tools) places an Amazon Connect call to the emergency contact and ranks doctors in DynamoDB. Nothing dispatches an ambulance: the worker asks the patient for their WhatsApp location, sends it to the doctor and (as a template) to the emergency contact, and the doctor calls the ambulance.
7. **Doctor offers (ESI 1-3 and human review).** The worker offers the case to free doctors (one case each; only numbers the doctor confirmed) on WhatsApp and in the portal, 3 at once for ESI 1-2; the first to accept is assigned, and the doctor can then work the case from their own WhatsApp. A case nobody free takes waits: the portal alerts the administrators and, for ESI 1-2, the worker calls their phones through Amazon Connect (the same contact flow as the emergency-contact call). The API creates the Chime SDK meeting when the doctor starts a video call.
8. The worker writes cases and chat to DynamoDB and publishes to SNS `resq-portal-events`. Each API task subscribes its own SQS queue (`resq-api-events-*`) to that topic and pushes updates to its WebSocket clients.
9. **Failure path.** After 5 failed receives, a message moves to `resq-webhook-dlq`. A CloudWatch alarm then notifies SNS `resq-emergency-alerts`, which emails the on-call ops team.
10. **Deploys.** GitLab CI on `main` assumes `resq-gitlab-ci-deploy` through OIDC. It pushes both images to ECR and redeploys the two ECS services. It also builds the portal with the settings in SSM `/resq/ci/portal-deploy`, uploads it to S3 and invalidates CloudFront.

## Services
| Service | Purpose |
|---|---|
| CloudFront + S3 (portal) | SPA hosting; single origin for the portal, API and WebSockets |
| Cognito | Portal login; `Doctors` / `Administrators` groups; doctor invitation emails |
| ALB | CloudFront-only origin for the API service |
| ECS Fargate: API | FastAPI, WebSockets, Meta webhook (rollback), portal-event subscriber, Chime meetings, Cognito invitations (doctors and administrators), deleting closed cases (with their S3 media) |
| ECS Fargate: worker | SQS consumer: `app/pipeline.py` for WhatsApp messages, `app/tasks.py` for internal tasks and call events; the only service allowed to call Amazon Connect |
| End User Messaging Social | Production WhatsApp channel (`WHATSAPP_PROVIDER=eum_social`); templates are created by `whatsapp_templates.tf` |
| SNS / SQS | Inbound queue + DLQ (also carries delayed internal tasks), portal-event fan-out, ops alerts |
| DynamoDB `ResQTable` | Single table: users, doctors (with the doctor lock and number confirmation), administrators, cases (with offer and waiting state), chat, metrics, idempotency, direct-call links |
| S3 audio vault + Transcribe | Voice-note storage and speech to text |
| Bedrock AgentCore Runtime / Gateway / Memory | Hosted triage agent; MCP tool gateway; patient facts across cases |
| Bedrock | Primary and fallback models for triage and for reading registrations |
| Lambda `resq-gateway-tools` | 3 tools (outside the VPC) |
| Amazon Connect, Chime SDK | Emergency-contact call and the administrators' no-doctor call; doctor-patient video |
| ECR, Secrets Manager, SSM, CloudWatch, IAM | Images, WhatsApp secret, CI portal settings, logs and alarms, least-privilege roles (including the GitLab OIDC role) |

## Network page
The Terraform defaults are a NAT gateway per AZ and interface endpoints in the private subnets. This deployment sets `single_nat_gateway = true` and `enable_interface_endpoints = false`. So there is one NAT gateway, in `us-east-1a`. All AWS API traffic except S3 and DynamoDB (gateway endpoints) leaves through that NAT, as do End User Messaging Social, AgentCore, Chime, Connect, Cognito and the Meta Graph API. The faded icons on the page are the defaults that are not created here.

## Key design decisions
- The API and the worker are separate ECS services fed by SQS, so a burst of inbound messages can't starve the portal.
- Only ESI 1-2 cases reach the emergency tools. Each tool runs once per case (DynamoDB idempotency keys). Triage never assigns a doctor; the offer does, and a doctor holds one case at a time.
- The diagram leaves out the Meta webhook path, to keep the layout clean. That path runs from Meta through CloudFront, the ALB and the API into SQS, used locally and for rollback (see `docs/whatsapp-testing.md`). WhatsApp calling (`whatsapp_calling.tf`) is off until Meta allows it on the number.
