import React, { useState, useEffect, useRef } from 'react';
import { Mic, Send, Sparkles, Check, AlertCircle, Loader2 } from 'lucide-react';
import { api } from '../services/api';
import { CaseFlags } from './AuditLogs';
import { Badge, EsiBadge, StatusBadge, Modal } from './ui';

// `esi` is the expected triage outcome, shown as a hint on the preset card.
const PRESETS = [
  {
    title: "Cardiac emergency",
    lang: "Urdu",
    esi: 1,
    phone: "+923009988776",
    text: "Mere uncle ko dil ka daura par raha hai, seene me shadeed dard hai, please jaldi ambulance bhejein!"
  },
  {
    title: "Road accident trauma",
    lang: "Hindi",
    esi: 2,
    phone: "+919811234567",
    text: "Bhaiya highway par car accident ho gaya hai, sar se bahut khoon beh raha hai, jaldi ambulance bhejo!"
  },
  {
    title: "Acute stroke / collapse",
    lang: "English",
    esi: 2,
    phone: "+14155552671",
    text: "My mother suddenly collapsed, her face is drooping on one side and she cannot speak clearly, please help!"
  },
  {
    title: "Vomiting & stomach pain",
    lang: "Urdu",
    note: "Urgent, doctor review only",
    esi: 3,
    phone: "+923451234567",
    text: "Mere bete ko kal raat se ulti aur pet me dard ho raha hai."
  },
  {
    title: "Mild cold & headache",
    lang: "Urdu",
    note: "Non-emergency",
    esi: 5,
    phone: "+923214455667",
    text: "Mujhe kal se halka sa zukam aur sar dard hai, koi tablet bata dein please."
  }
];

const STATUS_POLL_MS = 1500;
const STATUS_POLL_TIMEOUT_MS = 120000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export default function SimulateVoiceModal({ onClose, onSimulated }) {
  const [phone, setPhone] = useState("+923009988776");
  const [voiceText, setVoiceText] = useState(PRESETS[0].text);
  const [language, setLanguage] = useState("Urdu");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [progress, setProgress] = useState(null); // queued-processing status text
  const [error, setError] = useState(null);
  const [noCaseNote, setNoCaseNote] = useState(null);
  const [action, setAction] = useState(null); // triaged | human_review | handover | ...
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Queued mode (SQS + worker): poll the message status until the worker has processed it.
  const waitForCase = async (providerMessageId) => {
    const deadline = Date.now() + STATUS_POLL_TIMEOUT_MS;
    while (mountedRef.current && Date.now() < deadline) {
      try {
        const st = await api.getMessageStatus(providerMessageId);
        if (!mountedRef.current) return null;
        if (st?.status === 'PROCESSED') {
          if (!st.case_id) return { noCase: true, action: st.action };
          return { case: await api.getCaseDetail(st.case_id), action: st.action };
        }
        setProgress(st?.status === 'IN_PROGRESS' ? 'Worker is triaging the voice note...' : 'Queued, waiting for the worker...');
      } catch (err) {
        // 404 = not recorded yet; transient errors: keep polling until the deadline
        if (![404, 502, 503, 504].includes(err?.response?.status) && err?.response) throw err;
      }
      await sleep(STATUS_POLL_MS);
    }
    if (mountedRef.current) throw new Error('Timed out waiting for the worker to process the message.');
    return null;
  };

  const handleSelectPreset = (p) => {
    setPhone(p.phone);
    setVoiceText(p.text);
    setLanguage(p.lang);
  };

  const handleTriggerSimulation = async (e) => {
    e.preventDefault();
    if (!voiceText.trim()) return;

    setLoading(true);
    setResult(null);
    setError(null);
    setNoCaseNote(null);
    setAction(null);
    setProgress(null);
    try {
      const res = await api.simulateVoiceNote(phone, voiceText, language);
      let caseData = res?.case || null;
      let outcomeAction = res?.action || null;
      if (!caseData && res?.provider_message_id) {
        setProgress('Queued, waiting for the worker...');
        const outcome = await waitForCase(res.provider_message_id);
        if (!mountedRef.current || !outcome) return;
        if (outcome.noCase) {
          setNoCaseNote(`Message processed${outcome.action ? ` (${outcome.action})` : ''}: no case was created (e.g. it was handled as registration).`);
        }
        caseData = outcome.case || null;
        outcomeAction = outcome.action || outcomeAction;
      }
      if (!mountedRef.current) return;
      setAction(outcomeAction);
      setResult(caseData);
      if (onSimulated) onSimulated(caseData);
    } catch (err) {
      console.error("Simulation failed", err);
      if (mountedRef.current) setError(err?.message || 'Simulation failed.');
    } finally {
      if (mountedRef.current) {
        setLoading(false);
        setProgress(null);
      }
    }
  };

  return (
    <Modal
      title="Simulate WhatsApp voice note"
      subtitle="Run a multilingual emergency through the triage pipeline (Urdu, Hindi or English)"
      icon={Mic}
      onClose={onClose}
    >
      <div className="min-h-0 space-y-6 overflow-y-auto px-5 py-5">

        {/* Quick Presets */}
        <div>
          <span className="mb-2 flex items-center gap-1.5 text-sm font-medium text-ink">
            <Sparkles className="h-4 w-4 text-slate-400" />
            Demo scenarios
          </span>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {PRESETS.map((p, idx) => {
              const selected = voiceText === p.text;
              return (
                <button
                  key={idx}
                  type="button"
                  onClick={() => handleSelectPreset(p)}
                  className={`rounded-lg border p-3 text-left transition-colors ${
                    selected
                      ? 'border-brand-500 bg-brand-50/60 ring-1 ring-brand-500'
                      : 'border-slate-200 hover:border-slate-300 hover:bg-canvas'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium text-ink">{p.title}</span>
                    <EsiBadge level={p.esi} />
                  </div>
                  <span className="mt-0.5 block text-xs text-slate-500">{p.lang}{p.note ? ` · ${p.note}` : ''}</span>
                  <span className="mt-1 line-clamp-1 block text-xs italic text-slate-500">"{p.text}"</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Form */}
        <form onSubmit={handleTriggerSimulation} className="space-y-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="sim-phone" className="label">Caller phone number</label>
              <input
                id="sim-phone"
                type="text"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                className="input font-mono"
              />
            </div>

            <div>
              <label htmlFor="sim-lang" className="label">Language spoken</label>
              <select
                id="sim-lang"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                className="input"
              >
                <option value="Urdu">Urdu (اردو)</option>
                <option value="Hindi">Hindi (हिन्दी)</option>
                <option value="English">English</option>
              </select>
            </div>
          </div>

          <div>
            <label htmlFor="sim-text" className="label">Voice note transcript</label>
            <textarea
              id="sim-text"
              rows="3"
              value={voiceText}
              onChange={(e) => setVoiceText(e.target.value)}
              className="input"
            />
          </div>

          <button type="submit" disabled={loading} className="btn btn-primary w-full py-2.5">
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            <span>{loading ? (progress || "Running AI triage...") : "Send voice note to ResQ-AI"}</span>
          </button>
        </form>

        {error && (
          <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            <AlertCircle className="h-4 w-4 shrink-0" /> {error}
          </div>
        )}
        {noCaseNote && !result && (
          <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-canvas p-3 text-sm text-slate-700">
            <Check className="h-4 w-4 shrink-0 text-brand-600" /> {noCaseNote}
          </div>
        )}

        {/* Handover: the phone already had an open case, so no new triage ran (FLOW-2) */}
        {result && action === 'handover' && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
            <span>
              This number already has an open case, so the voice note was added to the chat of case{' '}
              <span className="font-mono font-semibold">{result.case_id}</span> instead of being triaged again.
              Resolve that case in the Live Console to simulate a fresh emergency, or use another phone number.
            </span>
          </div>
        )}

        {/* Simulation Output Card */}
        {result && (
          <div className="overflow-hidden rounded-lg border border-slate-200">
            <div className="flex items-center justify-between gap-3 border-b border-slate-200 bg-canvas px-4 py-2.5">
              <span className="flex items-center gap-1.5 text-sm font-medium text-ink">
                <Check className="h-4 w-4 text-brand-600" />
                {action === 'handover'
                  ? `Added to open case ${result.case_id}`
                  : action === 'human_review'
                  ? 'Case created for human review'
                  : 'AI triage assessment completed'}
              </span>
              <EsiBadge level={result.esi_level} withName />
            </div>
            <dl className="divide-y divide-slate-100 px-4 text-sm">
              <ResultRow label="Status">
                <span className="inline-flex flex-wrap items-center justify-end gap-1.5">
                  <StatusBadge status={result.status} />
                  <CaseFlags caseData={result} />
                </span>
              </ResultRow>
              {result.clinical_summary && <ResultRow label="Summary">{result.clinical_summary}</ResultRow>}
              {result.assigned_doctor_name && <ResultRow label="Doctor">{result.assigned_doctor_name}</ResultRow>}
              {result.matched_specialty && <ResultRow label="Matched specialty">{result.matched_specialty}</ResultRow>}
              {result.location_requested_at && (
                <ResultRow label="Location">
                  <Badge tone="amber">Requested on WhatsApp</Badge>
                </ResultRow>
              )}
              {result.contact_call_id && (
                <ResultRow label="Amazon Connect call">
                  <span className="font-mono text-xs">{result.contact_call_id}</span> <Badge tone="brand">Dialing contact</Badge>
                </ResultRow>
              )}
            </dl>
          </div>
        )}

      </div>
    </Modal>
  );
}

function ResultRow({ label, children }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2.5">
      <dt className="shrink-0 text-slate-500">{label}</dt>
      <dd className="text-right text-ink">{children}</dd>
    </div>
  );
}
