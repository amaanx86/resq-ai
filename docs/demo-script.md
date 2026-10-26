# ResQ-AI: Pitch and Demo Script


1. **The problem:** in a heart attack or stroke, panic costs minutes. People do not navigate hospital portals, but everyone knows how to send a WhatsApp voice note.

2. **The demo:**
   - Open the portal (`http://localhost:5173` locally, or the CloudFront URL). Show the live queue.
   - If you already simulated from the same number and that case is still open, resolve it first, or the next message goes into its chat.
   - Optional, on a real phone with a new number: register an emergency contact by voice note ("my emergency contact is my brother Ali, 0333 ..."). The confirmation comes back and no case is opened.
   - Click **"Simulate voice note"** (or send a real voice note from a phone, see [whatsapp-testing.md](whatsapp-testing.md)) and pick the Urdu cardiac emergency:
     > *"Mere uncle ko dil ka daura par raha hai, seene me shadeed dard hai, please jaldi ambulance bhejein!"*
   - Show the result once the worker has processed it:
     - ESI 1 and the emergency contact call placed;
     - the WhatsApp reply and the "Send location" request in the chat;
     - the case offered to **Dr. Sarah Khan, MD (Cardiology)** and two other doctors (popup in the portal, Accept / Decline on their WhatsApp).
   - Accept the offer; the patient is told which doctor accepted. On a real doctor's phone: the doctor gets the case card, replies to the patient straight from WhatsApp, and sends `@resq video` to start the call from the phone.
   - Register a doctor with a real phone: it gets "Is this your number?" on WhatsApp; tap **Yes, it's me** and the roster shows "Number confirmed".
   - Show what happens with no doctor free: switch every doctor off duty and send an emergency. The patient is told to wait, the administrator portal shows the red banner with the alarm, and the administrator's phone rings. Switch a doctor back on duty: the case is offered to them at once.
   - On a real phone, share the location. It appears in the "Patient location & ambulance" tile with a map link and goes to the emergency contact on WhatsApp. The doctor calls 1122 and clicks **Mark ambulance called**, and the patient is told on WhatsApp. Nothing dispatches an ambulance automatically.
   - Open Case History to show the triage engine, the reasoning, each tool's result and timing, and the offer rounds.
   - In the console, type a message in the chat bridge (it goes to the patient's WhatsApp). Then click **"Start video call"** to show the Chime room and the WhatsApp join button. On a real phone, tap **Request a call** to show the doctor's **Open chat & call** link.
   - Contrast with the mild cold (ESI 5): no ambulance, no call, advice only. Optionally use the ESI 3 preset: a doctor reviews it, but there is no contact call and no location request.
   - Be accurate about what runs where. Locally, triage and registration use keyword readers, and Connect, Transcribe and Chime are simulated. Do not quote a response time until one has been measured on AWS (the analytics tab reports it).

3. **The architecture:** walk through [`architecture/resq-aws-infrastructure-overview.drawio.png`](../architecture/resq-aws-infrastructure-overview.drawio.png):
   - End User Messaging Social, through SNS and SQS, to the ECS worker;
   - the Strands agent on AgentCore Runtime, with AgentCore Memory, and its tools on the AgentCore Gateway (Lambda);
   - Amazon Transcribe, Amazon Connect + Polly, Chime SDK meetings;
   - the DynamoDB single table;
   - the React 19 portal on CloudFront with Cognito;
   - GitLab CI deploys through OIDC.

   [`architecture/resq-inbound-flow.drawio.png`](../architecture/resq-inbound-flow.drawio.png) shows the message routing, including registration and doctor offers.
