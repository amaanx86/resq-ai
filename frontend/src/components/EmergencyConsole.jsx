import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  PhoneCall, Truck, Video, Send, Volume2, MapPin, ExternalLink, Phone,
  ShieldAlert, CheckCircle2, User, MessageSquare, RefreshCw, CheckCheck, Loader2, Bot, ChevronRight,
  Check, X, UserPlus, Paperclip, Mic, Square, ImageIcon
} from 'lucide-react';
import { api } from '../services/api';
import { useReconnectingWebSocket } from '../hooks/useReconnectingWebSocket';
import { useVoiceRecorder } from '../hooks/useVoiceRecorder';
import {
  VoiceNotePlayer, PhotoAttachment, ImageAnalysisPanel, CaseFlags, TriageAuditPanel, PatientHistory, WhatsAppText
} from './AuditLogs';
import {
  Badge, EsiBadge, StatusBadge, LiveIndicator, PageHeader, EmptyState, esiStyle, formatTime, formatDateTime, timeAgo
} from './ui';
import { OfferBadge, formatCountdown, isOfferedTo, useCountdown } from './offers';

const MESSAGE_POLL_FALLBACK_MS = 4000;   // WebSocket down
const MESSAGE_POLL_LIVE_MS = 30000;      // WebSocket up: slow safety net only
const PHOTO_TYPES = 'image/jpeg,image/png,image/webp,image/gif,image/heic';

function formatDuration(totalSeconds) {
  const m = Math.floor(totalSeconds / 60);
  const sec = String(totalSeconds % 60).padStart(2, '0');
  return `${m}:${sec}`;
}

/** Why a photo / voice note did not go out, in the doctor's words. */
function mediaSendError(err) {
  const status = err?.response?.status;
  const detail = err?.response?.data?.detail;
  if (status === 413) return detail || 'The file is too large to send.';
  if (status === 415) return detail || 'This file cannot be sent on WhatsApp.';
  if (status === 409) return 'This case is closed.';
  if (status === 403) return 'You are not allowed to message this patient.';
  if (status === 502) return detail || 'WhatsApp did not accept the file. Please try again.';
  return 'Could not send. Please try again.';
}

/** Merge chat messages, dedupe by message_id, keep chronological order. */
function mergeMessages(prev, incoming) {
  const byId = new Map();
  [...prev, ...incoming].forEach((m) => {
    if (m && m.message_id) byId.set(m.message_id, { ...(byId.get(m.message_id) || {}), ...m });
  });
  return [...byId.values()].sort((a, b) => new Date(a.timestamp || 0) - new Date(b.timestamp || 0));
}

function findTool(caseData, names) {
  return (caseData?.tool_results || []).find((t) => names.includes(t.tool || t.name));
}

/** Same format the backend puts in the chat (app.pipeline.maps_url). */
function mapsUrl(lat, lng) {
  return `https://maps.google.com/?q=${Number(lat).toFixed(6)},${Number(lng).toFixed(6)}`;
}

function hasLocation(c) {
  return c?.location_latitude != null && c?.location_longitude != null;
}

const LOCATION_ALERT_TEXT = {
  SENT: 'Location sent on WhatsApp',
  FAILED: 'Location not delivered on WhatsApp',
  SKIPPED_NO_CONTACT: 'No contact to send the location to',
};

export default function EmergencyConsole({
  cases = [],
  casesLive = false,
  onRefreshCases,
  onCaseResolved,
  onCaseUpdated,
  onOpenVideoCall,
  focusCaseId = null,
  onFocusHandled,
  user = null,
  authEnabled = false,
}) {
  const [selectedId, setSelectedId] = useState(null);
  const [doctors, setDoctors] = useState([]);
  const [messages, setMessages] = useState([]);
  const [newMessage, setNewMessage] = useState('');
  // A photo or voice note waiting to be sent (IMAGE-1): {file, kind: 'image' | 'audio', previewUrl}.
  const [attachment, setAttachment] = useState(null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState(null);
  const fileInputRef = useRef(null);
  const [resolving, setResolving] = useState(false);
  const [resolveError, setResolveError] = useState(null);
  const [markingAmbulance, setMarkingAmbulance] = useState(false);
  const [ambulanceError, setAmbulanceError] = useState(null);
  const [startingCall, setStartingCall] = useState(false);
  const [callError, setCallError] = useState(null);
  const chatScrollRef = useRef(null);

  const selectedCase = cases.find((c) => c.case_id === selectedId) || null;
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;

  // Open a case from a `?case=` link or an accepted offer once it is in the queue.
  useEffect(() => {
    if (focusCaseId && cases.some((c) => c.case_id === focusCaseId)) {
      setSelectedId(focusCaseId);
      onFocusHandled?.();
    }
  }, [focusCaseId, cases, onFocusHandled]);

  // Doctor names for the offer card, and the admin's "Assign doctor" list.
  useEffect(() => {
    api.getDoctors().then(setDoctors).catch(() => setDoctors([]));
  }, []);

  // Keep a valid selection as the queue changes (new cases, resolved cases).
  useEffect(() => {
    if (!selectedCase && cases.length > 0) setSelectedId(cases[0].case_id);
    if (cases.length === 0 && selectedId) setSelectedId(null);
  }, [cases, selectedCase, selectedId]);

  const loadMessages = useCallback(async (caseId) => {
    try {
      const msgs = await api.getCaseMessages(caseId);
      if (selectedIdRef.current !== caseId) return; // stale response for a previous selection
      setMessages((prev) => {
        const sameCase = prev.filter((m) => m.case_id === caseId);
        return mergeMessages(sameCase, msgs);
      });
    } catch (err) {
      console.error("Failed to load messages", err);
    }
  }, []);

  // Per-case live chat bridge (BRIDGE-1).
  const handleBridgeEvent = useCallback((event) => {
    if (event.type === 'chat_message' && event.message && event.case_id === selectedId) {
      setMessages((prev) => mergeMessages(prev, [event.message]));
    }
  }, [selectedId]);

  const bridgeLive = useReconnectingWebSocket(
    selectedId ? `/ws/bridge/${encodeURIComponent(selectedId)}` : null,
    handleBridgeEvent
  );

  useEffect(() => {
    setMessages([]);
    setResolveError(null);
    setAmbulanceError(null);
    setCallError(null);
    if (selectedId) loadMessages(selectedId);
  }, [selectedId, loadMessages]);

  // Polling fallback: fast while the socket is down, slow while it is up. Re-armed when the
  // socket state changes, without clearing the chat.
  useEffect(() => {
    if (!selectedId) return undefined;
    if (bridgeLive) loadMessages(selectedId); // catch up on anything missed while reconnecting
    const interval = setInterval(
      () => loadMessages(selectedId),
      bridgeLive ? MESSAGE_POLL_LIVE_MS : MESSAGE_POLL_FALLBACK_MS
    );
    return () => clearInterval(interval);
  }, [bridgeLive, selectedId, loadMessages]);

  // Scroll only the message pane (scrollIntoView would also scroll the page).
  useEffect(() => {
    const el = chatScrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  const attach = useCallback((file, kind) => {
    setSendError(null);
    setAttachment((prev) => {
      if (prev?.previewUrl) URL.revokeObjectURL(prev.previewUrl);
      return file ? { file, kind, previewUrl: URL.createObjectURL(file) } : null;
    });
  }, []);

  const recorder = useVoiceRecorder((file) => attach(file, 'audio'));
  const cancelRecording = recorder.cancel;

  // A pending attachment belongs to the case it was made for.
  useEffect(() => {
    attach(null);
    cancelRecording();
  }, [selectedId, attach, cancelRecording]);

  const handlePickPhoto = (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';   // picking the same file again still fires onChange
    if (file) attach(file, 'image');
  };

  const handleSendMessage = async (e) => {
    e.preventDefault();
    if (!selectedCase || sending || recorder.recording) return;
    if (!attachment && !newMessage.trim()) return;
    const senderName = selectedCase.assigned_doctor_name || "Dr. On-Duty Specialist";

    setSending(true);
    setSendError(null);
    try {
      const sent = attachment
        ? await api.sendDoctorMedia(selectedCase.case_id, attachment.file, newMessage.trim(), senderName)
        : await api.sendDoctorMessage(selectedCase.case_id, newMessage, senderName);
      setMessages((prev) => mergeMessages(prev, [sent]));
      setNewMessage('');
      attach(null);
    } catch (err) {
      console.error("Failed to bridge message to WhatsApp", err);
      setSendError(attachment ? mediaSendError(err) : 'Could not send the message. Please try again.');
    } finally {
      setSending(false);
    }
  };

  const handleResolve = async () => {
    if (!selectedCase) return;
    if (!window.confirm(`Mark case ${selectedCase.case_id} (${selectedCase.patient_name}) as resolved? New messages from this patient will then start a new case.`)) {
      return;
    }
    setResolving(true);
    setResolveError(null);
    try {
      const resolved = await api.resolveCase(selectedCase.case_id);
      if (onCaseResolved) onCaseResolved(resolved && resolved.case_id ? resolved : { ...selectedCase, status: 'RESOLVED' });
    } catch (err) {
      const status = err?.response?.status;
      setResolveError(
        status === 403 ? 'You are not allowed to resolve this case.'
          : status === 404 || status === 405 ? 'Resolve is not available on this backend yet.'
          : 'Could not resolve the case. Please try again.'
      );
    } finally {
      setResolving(false);
    }
  };

  const handleAmbulanceCalled = async () => {
    if (!selectedCase) return;
    setMarkingAmbulance(true);
    setAmbulanceError(null);
    try {
      const updated = await api.markAmbulanceCalled(selectedCase.case_id);
      if (onCaseUpdated && updated?.case_id) onCaseUpdated(updated);
    } catch (err) {
      const status = err?.response?.status;
      setAmbulanceError(
        status === 403 ? 'You are not allowed to update this case.'
          : status === 409 ? 'This case is closed.'
          : 'Could not record the ambulance call. Please try again.'
      );
    } finally {
      setMarkingAmbulance(false);
    }
  };

  // Direct WhatsApp call: calls cannot ring in the portal yet, so the doctor calls the patient from
  // their own WhatsApp. The tab opens before the request so popup blockers allow it.
  const handleDirectCall = async () => {
    if (!selectedCase) return;
    const ok = window.confirm("This opens the patient's WhatsApp chat so you can call them from your own "
      + 'WhatsApp. The patient will see your number, and gets a message that you are calling. Continue?');
    if (!ok) return;
    const tab = window.open('', '_blank');
    setStartingCall(true);
    setCallError(null);
    try {
      const { url } = await api.startDirectCall(selectedCase.case_id);
      if (tab) tab.location.href = url;
      else window.location.href = url;
    } catch (err) {
      if (tab) tab.close();
      const status = err?.response?.status;
      setCallError(
        status === 409 ? (err?.response?.data?.detail || 'This case cannot be called right now.')
          : status === 403 ? 'You are not allowed to call this patient.'
          : 'Could not open the WhatsApp call. Please try again.'
      );
    } finally {
      setStartingCall(false);
    }
  };

  // OFFER-1: until a doctor accepts, an offered doctor sees the case read only.
  const isAdmin = !authEnabled || Boolean(user?.isAdmin);
  const doctorId = user?.doctorId || null;
  const canAct = Boolean(selectedCase) && (isAdmin || selectedCase.assigned_doctor_id === doctorId);
  const actHint = canAct ? undefined : 'Accept the case to act on it';

  const contactTool = findTool(selectedCase, ['alert_emergency_contact', 'alert_emergency_contact_tool', 'call_emergency_contact']);
  const contactDetail = [
    selectedCase?.contact_call_id ? `Call ${selectedCase.contact_call_id}` : (contactTool?.status || 'No call placed'),
    LOCATION_ALERT_TEXT[selectedCase?.location_alert_status],
  ].filter(Boolean).join(' · ');

  return (
    <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">

      <PageHeader
        title="Live Console"
        description="Active WhatsApp emergencies, triaged by the Strands agent on Amazon Bedrock."
        actions={
          <>
            <LiveIndicator
              live={casesLive}
              liveLabel="Live feed"
              offLabel="Polling"
              title={casesLive ? 'Receiving case updates over WebSocket' : 'WebSocket unavailable: refreshing by polling'}
            />
            <button onClick={onRefreshCases} className="btn btn-secondary btn-sm ml-2">
              <RefreshCw className="h-3.5 w-3.5" />
              Refresh
            </button>
          </>
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">

        {/* Left Column: Active Cases Queue (4 cols) */}
        <div className="lg:col-span-4">
          <div className="card overflow-hidden">
            <div className="card-header">
              <h2 className="card-title">Active queue</h2>
              <span className="text-xs tabular-nums text-slate-500">{cases.length} open</span>
            </div>

            {cases.length === 0 ? (
              <EmptyState
                icon={CheckCircle2}
                title="Queue is clear"
                description="No pending emergency cases. Emergency lines are standing by."
              />
            ) : (
              <ul className="max-h-[calc(100vh-14rem)] divide-y divide-slate-100 overflow-y-auto">
                {cases.map((c) => {
                  const isSelected = selectedId === c.case_id;
                  return (
                    <li key={c.case_id}>
                      <button
                        type="button"
                        onClick={() => setSelectedId(c.case_id)}
                        className={`w-full border-l-[3px] px-4 py-3.5 text-left transition-colors ${esiStyle(c.esi_level).stripe} ${
                          isSelected ? 'bg-brand-50/60' : 'hover:bg-canvas'
                        }`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex min-w-0 items-center gap-2">
                            <EsiBadge level={c.esi_level} />
                            <span className="truncate text-xs text-slate-500">{c.matched_specialty || 'General ER'}</span>
                          </div>
                          <span className="shrink-0 text-xs tabular-nums text-slate-400" title={c.created_at ? new Date(c.created_at).toLocaleString() : ''}>
                            {timeAgo(c.created_at)}
                          </span>
                        </div>

                        <div className="mt-2 flex items-baseline justify-between gap-2">
                          <h3 className="truncate text-sm font-semibold text-brand-900">{c.patient_name}</h3>
                          <span className="shrink-0 font-mono text-xs text-slate-500">{c.user_phone}</span>
                        </div>

                        <p className="mt-1 line-clamp-2 text-sm leading-snug text-slate-600">{c.transcript}</p>

                        {(c.needs_human_review || c.degraded || c.doctor_offer_status) && (
                          <div className="mt-2 flex flex-wrap gap-1.5">
                            <OfferBadge caseData={c} />
                            <CaseFlags caseData={c} />
                          </div>
                        )}

                        <div className="mt-2.5 flex items-center justify-between text-xs">
                          {c.ambulance_called_at ? (
                            <span className="flex items-center gap-1.5 font-medium text-brand-700">
                              <Truck className="h-3.5 w-3.5" /> Ambulance called
                            </span>
                          ) : c.esi_level <= 2 ? (
                            <span className="flex items-center gap-1.5 font-medium text-amber-700">
                              <Truck className="h-3.5 w-3.5" /> Ambulance not called
                            </span>
                          ) : (
                            <span className="flex items-center gap-1.5 text-slate-500">
                              <Truck className="h-3.5 w-3.5" /> Doctor review
                            </span>
                          )}
                          <span className="flex items-center gap-2 text-slate-500">
                            {hasLocation(c) && (
                              <span className="flex items-center gap-0.5 text-brand-700" title="Location shared">
                                <MapPin className="h-3.5 w-3.5" /> Location
                              </span>
                            )}
                            {c.detected_language}
                          </span>
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>

        {/* Right Column: Active Emergency Case Workspace (8 cols) */}
        <div className="lg:col-span-8">
          {selectedCase ? (
            <div className="space-y-6">

              {/* Case header */}
              <div className="card">
                <div className="flex flex-wrap items-start justify-between gap-4 px-6 py-5">
                  <div className="min-w-0">
                    <span className="eyebrow">Case <span className="font-mono normal-case tracking-normal">{selectedCase.case_id}</span></span>
                    <h2 className="mt-1 text-xl font-semibold tracking-tight text-brand-900">{selectedCase.patient_name}</h2>
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <EsiBadge level={selectedCase.esi_level} withName />
                      {selectedCase.matched_specialty && <Badge tone="brand">{selectedCase.matched_specialty}</Badge>}
                      <StatusBadge status={selectedCase.status} />
                      <OfferBadge caseData={selectedCase} />
                      <CaseFlags caseData={selectedCase} showEngine />
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    {/* Resolve case (FLOW-2) */}
                    <button onClick={handleResolve} disabled={resolving || !canAct} title={actHint} className="btn btn-secondary">
                      {resolving ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCheck className="h-4 w-4" />}
                      <span>{resolving ? 'Resolving...' : 'Resolve case'}</span>
                    </button>

                    {/* Direct WhatsApp call (doctor's own number) */}
                    {callRequestPending(selectedCase) && (
                      <Badge tone="amber">Patient asked for a call</Badge>
                    )}
                    <button
                      onClick={handleDirectCall}
                      disabled={startingCall || !canAct || !selectedCase.assigned_doctor_id}
                      title={!selectedCase.assigned_doctor_id ? 'No doctor has accepted this case yet' : (actHint || 'Call the patient from your own WhatsApp')}
                      className="btn btn-secondary"
                    >
                      {startingCall ? <Loader2 className="h-4 w-4 animate-spin" /> : <Phone className="h-4 w-4" />}
                      <span>Call on WhatsApp</span>
                    </button>

                    {/* 1-Click Video Call Launcher */}
                    <button onClick={() => onOpenVideoCall(selectedCase)} disabled={!canAct} title={actHint} className="btn btn-primary">
                      <Video className="h-4 w-4" />
                      <span>Start video call</span>
                    </button>
                  </div>
                </div>
                {resolveError && (
                  <p className="-mt-2 px-6 pb-4 text-sm text-red-600">{resolveError}</p>
                )}
                {callError && (
                  <p className="-mt-2 px-6 pb-4 text-sm text-red-600">{callError}</p>
                )}

                <dl className="grid grid-cols-2 gap-px border-t border-slate-200 bg-slate-200 sm:grid-cols-4">
                  {[
                    ['Phone', <span className="font-mono">{selectedCase.user_phone || '—'}</span>],
                    ['Language', selectedCase.detected_language || '—'],
                    ['Received', formatDateTime(selectedCase.received_at || selectedCase.created_at) || '—'],
                    ['Status', <span className="capitalize">{String(selectedCase.status || '—').replace(/_/g, ' ').toLowerCase()}</span>],
                  ].map(([label, value]) => (
                    <div key={label} className="bg-white px-6 py-3">
                      <dt className="text-xs text-slate-500">{label}</dt>
                      <dd className="mt-0.5 truncate text-sm font-medium text-ink">{value}</dd>
                    </div>
                  ))}
                </dl>
              </div>

              {/* Patient location and the doctor's ambulance call (no automatic dispatch) */}
              <LocationAmbulanceCard
                caseData={selectedCase}
                marking={markingAmbulance}
                error={ambulanceError}
                onMarkCalled={handleAmbulanceCalled}
                canAct={canAct}
              />

              {/* Automated response actions */}
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <ActionTile
                  icon={PhoneCall}
                  label="Emergency contact"
                  done={Boolean(selectedCase.contact_call_id)}
                  value={<span className="font-mono">{selectedCase.emergency_contact_phone || 'Not registered'}</span>}
                  detail={contactDetail}
                />
                <ActionTile
                  icon={User}
                  label="Assigned specialist"
                  done={Boolean(selectedCase.assigned_doctor_name)}
                  doneTone="brand"
                  value={selectedCase.assigned_doctor_name || offerSummary(selectedCase)}
                  detail={selectedCase.matched_specialty || null}
                />
              </div>

              {/* Doctor case offer (OFFER-1 / OFFER-2) */}
              {!selectedCase.assigned_doctor_id && selectedCase.doctor_offer_status && (
                <DoctorOfferCard
                  key={`${selectedCase.case_id}-${selectedCase.offer_id || ''}`}
                  caseData={selectedCase}
                  doctors={doctors}
                  offeredToMe={isOfferedTo(selectedCase, doctorId)}
                  doctorId={doctorId}
                  isAdmin={isAdmin}
                  onUpdated={onCaseUpdated}
                />
              )}

              {/* Patient report */}
              <div className="card">
                <div className="card-header">
                  <h3 className="card-title flex items-center gap-2">
                    {selectedCase.image_s3_key && !selectedCase.audio_s3_key ? (
                      <><ImageIcon className="h-4 w-4 text-slate-500" /> Patient's photo</>
                    ) : (
                      <><Volume2 className="h-4 w-4 text-slate-500" /> Inbound voice note</>
                    )}
                  </h3>
                  {!selectedCase.audio_s3_key && !selectedCase.image_s3_key && (
                    <span className="text-xs text-slate-400">No recording (simulated or text message)</span>
                  )}
                </div>
                <div className="space-y-4 px-5 py-4">
                  <div>
                    <VoiceNotePlayer
                      key={selectedCase.case_id}
                      caseId={selectedCase.case_id}
                      hasAudio={Boolean(selectedCase.audio_s3_key)}
                    />
                    <PhotoAttachment
                      key={`photo-${selectedCase.case_id}`}
                      caseId={selectedCase.case_id}
                      hasImage={Boolean(selectedCase.image_s3_key)}
                    />
                    <blockquote className="mt-2 whitespace-pre-line border-l-2 border-slate-300 pl-4 text-[15px] leading-relaxed text-slate-800">
                      {selectedCase.transcript}
                    </blockquote>
                  </div>

                  {/* What the image analysis agent saw in the photo (IMAGE-1) */}
                  <ImageAnalysisPanel caseData={selectedCase} />

                  {/* Patient history from AgentCore Memory (MEM-1), shown up front for the doctor */}
                  {(selectedCase.patient_history || []).length > 0 && (
                    <PatientHistory caseData={selectedCase} />
                  )}

                  {/* Agent reasoning (DATA-2) */}
                  {(selectedCase.clinical_summary || selectedCase.reasoning || (selectedCase.tool_results || []).length > 0) && (
                    <details className="group rounded-lg border border-slate-200">
                      <summary className="flex cursor-pointer select-none list-none items-center gap-2 px-4 py-3 text-sm font-medium text-slate-700 hover:text-ink [&::-webkit-details-marker]:hidden">
                        <ChevronRight className="h-4 w-4 text-slate-400 transition-transform group-open:rotate-90" />
                        Agent assessment, reasoning and tool results
                      </summary>
                      <div className="border-t border-slate-200 p-4">
                        <TriageAuditPanel caseData={selectedCase} showHistory={false} />
                      </div>
                    </details>
                  )}
                </div>
              </div>

              {/* WhatsApp Live Chat Bridge */}
              <div className="card flex h-[460px] flex-col overflow-hidden">

                {/* Chat Header */}
                <div className="card-header">
                  <h3 className="card-title flex items-center gap-2">
                    <MessageSquare className="h-4 w-4 text-slate-500" />
                    WhatsApp conversation
                  </h3>
                  <LiveIndicator
                    live={bridgeLive}
                    liveLabel="Live channel"
                    offLabel="Reconnecting (polling)"
                    title={bridgeLive ? undefined : 'WebSocket unavailable, reconnecting; messages refresh every few seconds'}
                  />
                </div>

                {/* Messages Stream */}
                <div ref={chatScrollRef} className="flex-1 space-y-3 overflow-y-auto bg-canvas px-5 py-4">
                  {messages.length === 0 && (
                    <p className="py-8 text-center text-sm text-slate-400">No messages on this case yet.</p>
                  )}
                  {messages.map((m) => {
                    const isDoctor = m.sender === "DOCTOR";
                    const isBot = m.sender === "BOT";
                    return (
                      <div
                        key={m.message_id}
                        className={`flex flex-col ${
                          isDoctor ? 'items-end' : (isBot ? 'items-center' : 'items-start')
                        }`}
                      >
                        <div
                          className={`text-sm ${
                            isDoctor
                              ? 'max-w-[75%] rounded-2xl rounded-br-md bg-brand-600 px-3.5 py-2.5 text-white shadow-sm'
                              : isBot
                              ? 'max-w-[85%] rounded-lg border border-dashed border-slate-300 bg-white/70 px-3.5 py-2.5 text-slate-600'
                              : 'max-w-[75%] rounded-2xl rounded-bl-md bg-white px-3.5 py-2.5 text-slate-800 shadow-sm ring-1 ring-slate-200'
                          }`}
                        >
                          {isBot ? (
                            <span className="mb-1 flex items-center gap-1 text-xs font-medium text-slate-500">
                              <Bot className="h-3.5 w-3.5" /> {m.sender_name || 'ResQ-AI'}
                              <span className="font-normal text-slate-400">· automated reply</span>
                            </span>
                          ) : (
                            <span className={`mb-0.5 block text-xs font-semibold ${isDoctor ? 'text-brand-100' : 'text-slate-500'}`}>
                              {m.sender_name}
                            </span>
                          )}
                          <p className="leading-relaxed"><WhatsAppText text={m.content} /></p>
                          <VoiceNotePlayer
                            caseId={m.case_id || selectedCase.case_id}
                            messageId={m.message_id}
                            hasAudio={Boolean(m.audio_s3_key)}
                            compact
                          />
                          <PhotoAttachment
                            caseId={m.case_id || selectedCase.case_id}
                            messageId={m.message_id}
                            hasImage={Boolean(m.image_s3_key)}
                            compact
                          />
                        </div>
                        <span className="mt-1 px-1 text-[11px] tabular-nums text-slate-400">
                          {formatTime(m.timestamp)}
                        </span>
                      </div>
                    );
                  })}
                </div>

                {/* Doctor Input Box: text, a photo or a voice note (IMAGE-1) */}
                <form onSubmit={handleSendMessage} className="border-t border-slate-200 bg-white p-3">
                  {attachment && (
                    <div className="mb-2 flex items-center gap-3 rounded-lg border border-slate-200 bg-canvas p-2">
                      {attachment.kind === 'image' ? (
                        <img src={attachment.previewUrl} alt="Photo to send" className="h-14 w-14 rounded-md object-cover ring-1 ring-slate-200" />
                      ) : (
                        <audio controls src={attachment.previewUrl} className="h-8 w-56 max-w-full" />
                      )}
                      <span className="min-w-0 flex-1 truncate text-xs text-slate-500">
                        {attachment.kind === 'image'
                          ? 'Photo ready. Your text is sent as its caption.'
                          : 'Voice note ready. Your text is sent with it.'}
                      </span>
                      <button
                        type="button"
                        onClick={() => attach(null)}
                        disabled={sending}
                        className="btn btn-ghost h-8 w-8 shrink-0 p-0"
                        title="Remove"
                        aria-label="Remove attachment"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  )}
                  {recorder.recording && (
                    <div className="mb-2 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                      <span className="h-2 w-2 animate-pulse rounded-full bg-red-600" />
                      Recording {formatDuration(recorder.seconds)}
                      <button type="button" onClick={recorder.cancel} className="ml-auto text-xs font-medium text-red-700 underline">
                        Discard
                      </button>
                    </div>
                  )}
                  {(sendError || recorder.error) && (
                    <p className="mb-2 text-xs text-red-600">{sendError || recorder.error}</p>
                  )}
                  <div className="flex gap-2">
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept={PHOTO_TYPES}
                      onChange={handlePickPhoto}
                      className="hidden"
                    />
                    <button
                      type="button"
                      onClick={() => fileInputRef.current?.click()}
                      disabled={!canAct || sending || recorder.recording}
                      title={canAct ? 'Send a photo' : actHint}
                      aria-label="Send a photo"
                      className="btn btn-secondary px-3"
                    >
                      <Paperclip className="h-4 w-4" />
                    </button>
                    {recorder.supported && (
                      <button
                        type="button"
                        onClick={recorder.recording ? recorder.stop : recorder.start}
                        disabled={!canAct || sending}
                        title={recorder.recording ? 'Stop recording' : (canAct ? 'Record a voice note' : actHint)}
                        aria-label={recorder.recording ? 'Stop recording' : 'Record a voice note'}
                        className={`btn px-3 ${recorder.recording ? 'btn-danger' : 'btn-secondary'}`}
                      >
                        {recorder.recording ? <Square className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
                      </button>
                    )}
                    <input
                      type="text"
                      value={newMessage}
                      onChange={(e) => setNewMessage(e.target.value)}
                      placeholder={!canAct ? 'Accept the case to message the patient'
                        : attachment ? 'Add a caption (optional)...' : "Write clinical instructions to the patient's WhatsApp..."}
                      disabled={!canAct}
                      className="input min-w-0 flex-1"
                    />
                    <button
                      type="submit"
                      disabled={(!newMessage.trim() && !attachment) || !canAct || sending || recorder.recording}
                      className="btn btn-primary"
                    >
                      {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                      <span className="hidden sm:inline">Send</span>
                    </button>
                  </div>
                </form>

              </div>

            </div>
          ) : (
            <div className="card">
              <EmptyState
                icon={ShieldAlert}
                title="No case selected"
                description="Select an emergency case from the queue to open its workspace."
                className="py-24"
              />
            </div>
          )}
        </div>

      </div>
    </div>
  );
}

// The patient tapped Request a call (or called the ResQ number) after the doctor last called them.
function callRequestPending(caseData) {
  if (!caseData?.call_requested_at) return false;
  return !caseData.direct_call_at || new Date(caseData.direct_call_at) < new Date(caseData.call_requested_at);
}

function LocationAmbulanceCard({ caseData, marking, error, onMarkCalled, canAct = true }) {
  const shared = hasLocation(caseData);
  const place = [caseData.location_name, caseData.location_address].filter(Boolean).join(', ');
  const called = Boolean(caseData.ambulance_called_at);
  return (
    <div className="card">
      <div className="card-header">
        <h3 className="card-title flex items-center gap-2">
          <MapPin className="h-4 w-4 text-slate-500" />
          Patient location &amp; ambulance
        </h3>
        <span className="text-xs text-slate-400">No automatic dispatch: the doctor calls the ambulance</span>
      </div>
      <div className="flex flex-wrap items-start justify-between gap-4 px-5 py-4">
        <div className="min-w-0">
          {shared ? (
            <>
              <p className="text-sm font-semibold text-ink">{place || 'Shared location'}</p>
              <p className="mt-0.5 font-mono text-xs text-slate-500">
                {Number(caseData.location_latitude).toFixed(6)}, {Number(caseData.location_longitude).toFixed(6)}
                {caseData.location_shared_at && (
                  <span className="font-sans"> · shared {formatDateTime(caseData.location_shared_at)}</span>
                )}
              </p>
              <a
                href={mapsUrl(caseData.location_latitude, caseData.location_longitude)}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-2 inline-flex items-center gap-1 text-sm font-medium text-brand-700 hover:text-brand-800"
              >
                Open in Google Maps <ExternalLink className="h-3.5 w-3.5" />
              </a>
            </>
          ) : (
            <p className="text-sm text-slate-500">
              {caseData.location_requested_at
                ? 'Waiting for the patient to share their location on WhatsApp.'
                : 'Location not requested (only ESI 1-2 cases ask for it).'}
            </p>
          )}
        </div>

        <div className="flex flex-col items-end gap-2">
          <div className="flex flex-wrap items-center justify-end gap-2">
            <a href="tel:1122" className="btn btn-secondary" title="Doctor: call Rescue 1122 for the patient">
              <Phone className="h-4 w-4" />
              <span>Call 1122</span>
            </a>
            {called ? (
              <Badge tone="green" icon={Truck}>
                Ambulance called {formatDateTime(caseData.ambulance_called_at)}
                {caseData.ambulance_called_by ? ` by ${caseData.ambulance_called_by}` : ''}
              </Badge>
            ) : (
              <button onClick={onMarkCalled} disabled={marking || !canAct} title={canAct ? undefined : 'Accept the case to act on it'} className="btn btn-primary">
                {marking ? <Loader2 className="h-4 w-4 animate-spin" /> : <Truck className="h-4 w-4" />}
                <span>{marking ? 'Saving...' : 'Mark ambulance called'}</span>
              </button>
            )}
          </div>
          {!called && (
            <p className="text-xs text-slate-500">After you call, mark it here: the patient is told on WhatsApp.</p>
          )}
          {error && <p className="text-xs text-red-600">{error}</p>}
        </div>
      </div>
    </div>
  );
}

function offerSummary(caseData) {
  if (caseData.doctor_offer_status === 'PENDING') return 'Waiting for a doctor to accept';
  if (caseData.doctor_offer_status === 'EXHAUSTED') {
    return caseData.no_doctor_reason === 'ALL_DECLINED' ? 'All doctors declined' : 'No doctor free';
  }
  return 'Unassigned';
}

const OUTCOME_TEXT = {
  PENDING: 'waiting', ACCEPTED: 'accepted', DECLINED: 'declined', TIMED_OUT: 'no answer',
  SUPERSEDED: 'replaced (case escalated)', ASSIGNED_BY_ADMIN: 'assigned by an admin',
  HANDED_OVER: 'handed over by the doctor',
};

/** Offer status for a case without a doctor: who it is offered to, the countdown, the rounds so far.
 *  An offered doctor accepts or declines here; an administrator can assign any active doctor. */
function DoctorOfferCard({ caseData, doctors, offeredToMe, doctorId, isAdmin, onUpdated }) {
  const pending = caseData.doctor_offer_status === 'PENDING';
  const seconds = useCountdown(pending ? caseData.offer_expires_at : null);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [assignee, setAssignee] = useState('');
  const nameOf = (id) => doctors.find((d) => d.doctor_id === id)?.name || id;
  const activeDoctors = doctors.filter((d) => d.is_active);

  const run = async (action, fn) => {
    setBusy(action);
    setError(null);
    try {
      const updated = await fn();
      if (updated?.case_id) onUpdated?.(updated);
    } catch (err) {
      const status = err?.response?.status;
      const detail = err?.response?.data?.detail;
      setError(status === 409 ? (typeof detail === 'string' && detail ? detail
        : 'This offer is no longer open (taken, reassigned or expired).')
        : status === 403 ? 'You are not allowed to do this.'
        : 'Something went wrong. Please try again.');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="card">
      <div className="card-header">
        <h3 className="card-title flex items-center gap-2">
          <UserPlus className="h-4 w-4 text-slate-500" />
          Doctor offer
        </h3>
        {pending && seconds != null && (
          <span className="font-mono text-sm font-semibold tabular-nums text-amber-700" title="Time left in this round">
            {formatCountdown(seconds)}
          </span>
        )}
      </div>
      <div className="space-y-3 px-5 py-4">
        <p className="text-sm text-slate-700">
          {pending
            ? <>Offered to <span className="font-medium text-ink">{(caseData.offered_doctor_ids || []).map(nameOf).join(', ')}</span>. The first doctor to accept takes the case.</>
            : <>No free doctor took this case ({caseData.no_doctor_reason === 'ALL_DECLINED' ? 'all doctors declined or did not answer' : 'nobody on duty, or all busy'}).
                The patient was told to wait and to call the emergency number if it gets worse. It is offered again as soon as a
                doctor is free; assign one by hand if it cannot wait.</>}
        </p>

        {(caseData.offer_history || []).length > 0 && (
          <ol className="space-y-1 text-xs text-slate-500">
            {caseData.offer_history.map((r, i) => (
              <li key={r.offer_id || i}>
                Round {i + 1}: {(r.doctor_ids || []).map(nameOf).join(', ')} · {OUTCOME_TEXT[r.outcome] || r.outcome}
                {r.doctor_id && r.outcome === 'ACCEPTED' ? ` by ${nameOf(r.doctor_id)}` : ''}
              </li>
            ))}
          </ol>
        )}

        {offeredToMe && (
          <div className="flex flex-wrap gap-2">
            <button onClick={() => run('accept', () => api.acceptOffer(caseData.case_id, caseData.offer_id, doctorId))}
              disabled={Boolean(busy) || seconds === 0} className="btn btn-primary">
              {busy === 'accept' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
              <span>Accept case</span>
            </button>
            <button onClick={() => run('decline', () => api.declineOffer(caseData.case_id, caseData.offer_id, doctorId))}
              disabled={Boolean(busy)} className="btn btn-secondary">
              {busy === 'decline' ? <Loader2 className="h-4 w-4 animate-spin" /> : <X className="h-4 w-4" />}
              <span>Decline</span>
            </button>
          </div>
        )}

        {isAdmin && pending && !offeredToMe && (
          <div className="flex flex-wrap gap-2">
            {(caseData.offered_doctor_ids || [])
              .filter((id) => !(caseData.declined_doctor_ids || []).includes(id))
              .map((id) => (
                <button key={id} onClick={() => run(`accept-${id}`, () => api.acceptOffer(caseData.case_id, caseData.offer_id, id))}
                  disabled={Boolean(busy)} className="btn btn-secondary btn-sm"
                  title="Record the acceptance for this doctor (e.g. they answered by phone)">
                  {busy === `accept-${id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                  <span>Accept for {nameOf(id)}</span>
                </button>
              ))}
          </div>
        )}

        {isAdmin && (
          <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
            <select value={assignee} onChange={(e) => setAssignee(e.target.value)} className="input w-auto py-1.5">
              <option value="">Assign a doctor...</option>
              {activeDoctors.map((d) => (
                // One case per doctor: a doctor on another case cannot be assigned.
                <option key={d.doctor_id} value={d.doctor_id}
                        disabled={d.busy_kind === 'CASE' && d.busy_case_id !== caseData.case_id}>
                  {d.name} ({d.specialty}){d.busy_kind === 'CASE' && d.busy_case_id !== caseData.case_id ? ' · on a case' : ''}
                </option>
              ))}
            </select>
            <button onClick={() => run('assign', () => api.assignCase(caseData.case_id, assignee))}
              disabled={!assignee || Boolean(busy)} className="btn btn-secondary">
              {busy === 'assign' ? <Loader2 className="h-4 w-4 animate-spin" /> : <UserPlus className="h-4 w-4" />}
              <span>Assign</span>
            </button>
          </div>
        )}
        {error && <p className="text-sm text-red-600">{error}</p>}
      </div>
    </div>
  );
}

function ActionTile({ icon: Icon, label, value, detail, done, doneTone = 'green' }) {
  const chip = done
    ? (doneTone === 'brand' ? 'bg-brand-50 text-brand-600' : 'bg-accent-50 text-accent-700')
    : 'bg-slate-100 text-slate-400';
  return (
    <div className="card flex items-start gap-3 px-4 py-4">
      <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${chip}`}>
        <Icon className="h-[18px] w-[18px]" />
      </div>
      <div className="min-w-0">
        <span className="eyebrow">{label}</span>
        <p className={`mt-0.5 truncate text-sm font-semibold ${done ? 'text-ink' : 'text-slate-500'}`}>{value}</p>
        {detail && <p className="truncate text-xs text-slate-500">{detail}</p>}
      </div>
    </div>
  );
}
