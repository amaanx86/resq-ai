import React, { useEffect, useRef, useState } from 'react';
import { Video, Copy, Check, MessageSquare, ExternalLink, Loader2, AlertTriangle, RefreshCw, Send } from 'lucide-react';
import { api } from '../services/api';
import VideoRoom from './VideoRoom';
import { Modal } from './ui';

// Doctor's side of the case's Chime video room (VIDEO-1). Opening the modal creates or reuses
// the meeting and sends the patient the WhatsApp join link; the doctor joins the same room here.
export default function VideoCallModal({ caseData, onClose }) {
  const caseId = caseData?.case_id;
  const [session, setSession] = useState(null);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [copied, setCopied] = useState(false);
  const [sendingInvite, setSendingInvite] = useState(false);
  const [inviteSent, setInviteSent] = useState(false);
  // Shared across StrictMode's double effect run so the WhatsApp invite goes out once.
  const sessionPromise = useRef(null);

  useEffect(() => {
    let active = true;
    if (!sessionPromise.current) sessionPromise.current = api.initiateVideoCall(caseId);
    setError(null);
    sessionPromise.current
      .then((s) => { if (active) setSession(s); })
      .catch((err) => {
        console.error('Failed to start the video session', err);
        if (active) setError(err?.response?.data?.detail || 'Could not create the video room.');
      });
    return () => { active = false; };
  }, [caseId, attempt]);

  const retry = () => {
    sessionPromise.current = null;
    setSession(null);
    setAttempt((n) => n + 1);
  };

  const patientUrl = session?.patient_join_url || caseData?.video_room_url || '';

  const handleCopyLink = () => {
    if (!patientUrl) return;
    navigator.clipboard.writeText(patientUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 3000);
  };

  const handleResendWhatsApp = async () => {
    setSendingInvite(true);
    try {
      // Reuses the live meeting and re-sends the same link.
      const s = await api.initiateVideoCall(caseId);
      setSession((prev) => (prev && prev.meeting_id === s.meeting_id ? prev : s));
      setInviteSent(true);
      setTimeout(() => setInviteSent(false), 4000);
    } catch (err) {
      console.error('Failed to re-send video link', err);
    } finally {
      setSendingInvite(false);
    }
  };

  return (
    <Modal
      title={`Video consultation · ${caseData?.patient_name || 'Patient'}`}
      subtitle={`Case ${caseId} · ${caseData?.assigned_doctor_name || 'On-duty doctor'} · ${caseData?.matched_specialty || 'Emergency Medicine'}`}
      icon={Video}
      onClose={onClose}
      size="max-w-4xl"
      headerExtra={session?.doctor_join_url && (
        <a
          href={session.doctor_join_url}
          target="_blank"
          rel="noreferrer"
          title="Open the room in a new tab"
          className="btn btn-ghost btn-sm hidden sm:inline-flex"
        >
          <ExternalLink className="h-3.5 w-3.5" /> New tab
        </a>
      )}
    >
      {/* Video room */}
      <div className="relative h-[60vh] min-h-[22rem] bg-navy-950">
        {session ? (
          <VideoRoom
            key={session.meeting_id}
            meetingId={session.meeting_id}
            caseId={caseId}
            role="doctor"
            embedded
            onLeave={onClose}
          />
        ) : error ? (
          <div className="flex h-full flex-col items-center justify-center p-6 text-center">
            <AlertTriangle className="h-9 w-9 text-red-400" />
            <p className="mt-3 text-sm text-slate-100">{error}</p>
            <button onClick={retry} className="btn btn-sm mt-4 bg-white/10 text-white hover:bg-white/20">
              <RefreshCw className="h-4 w-4" /> Try again
            </button>
          </div>
        ) : (
          <div className="flex h-full flex-col items-center justify-center text-center">
            <Loader2 className="h-8 w-8 animate-spin text-brand-400" />
            <p className="mt-3 text-sm text-slate-300">Creating the video room and sending the WhatsApp link...</p>
          </div>
        )}
      </div>

      {/* WhatsApp Direct Action Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-white px-5 py-3.5">
        <div className="flex min-w-0 flex-1 items-center gap-2 text-sm">
          <span className="flex shrink-0 items-center gap-1.5 font-medium text-slate-700">
            <MessageSquare className="h-4 w-4 text-slate-400" /> Patient link
          </span>
          <input
            type="text"
            readOnly
            value={patientUrl || 'Generating...'}
            className="input min-w-0 select-all bg-canvas py-1.5 font-mono text-xs text-slate-600"
          />
        </div>

        <div className="flex items-center gap-2">
          <button onClick={handleCopyLink} disabled={!patientUrl} className="btn btn-secondary btn-sm">
            {copied ? <Check className="h-3.5 w-3.5 text-brand-600" /> : <Copy className="h-3.5 w-3.5" />}
            <span>{copied ? 'Copied' : 'Copy link'}</span>
          </button>

          <button onClick={handleResendWhatsApp} disabled={sendingInvite || !session} className="btn btn-success btn-sm">
            {sendingInvite ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
            <span>{inviteSent ? 'Sent to WhatsApp' : (sendingInvite ? 'Sending...' : 'Resend to WhatsApp')}</span>
          </button>
        </div>
      </div>
    </Modal>
  );
}
