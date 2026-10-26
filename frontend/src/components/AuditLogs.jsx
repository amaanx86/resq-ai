import React, { useState, useEffect } from 'react';
import {
  History, Search, MessageSquare, Volume2, Loader2, AlertTriangle, Cpu, Wrench, Brain,
  ClipboardList, CheckCircle2, XCircle, MinusCircle, FileSearch, ScanEye, OctagonAlert, ImageIcon, Trash2, X
} from 'lucide-react';
import { api } from '../services/api';
import { useAudioUrl, useMediaUrl } from '../hooks/useAudioUrl';
import { Badge, EsiBadge, StatusBadge, PageHeader, EmptyState, esiStyle, formatDateTime } from './ui';

// ==========================================
// Shared case-view pieces (also used by EmergencyConsole)
// ==========================================

// http(s) URLs, without trailing punctuation (e.g. the map link in a shared-location message).
const URL_PATTERN = /(https?:\/\/[^\s<>"']*[^\s<>"'.,;:!?)])/g;
const FORMAT_PATTERN = /(\*[^*\n]+\*|_[^_\n]+_|~[^~\n]+~)/g;

function formatted(text, keyPrefix) {
  return text.split(FORMAT_PATTERN).map((part, pi) => {
    const key = `${keyPrefix}-${pi}`;
    if (/^\*[^*]+\*$/.test(part)) return <strong key={key}>{part.slice(1, -1)}</strong>;
    if (/^_[^_]+_$/.test(part)) return <em key={key}>{part.slice(1, -1)}</em>;
    if (/^~[^~]+~$/.test(part)) return <s key={key}>{part.slice(1, -1)}</s>;
    return <React.Fragment key={key}>{part}</React.Fragment>;
  });
}

/** Renders WhatsApp-style text: *bold*, _italic_, ~strike~, line breaks and clickable http(s)
 *  links (no HTML injection). Links are split out first so underscores in a URL stay intact. */
export function WhatsAppText({ text }) {
  const lines = String(text ?? '').split('\n');
  return (
    <>
      {lines.map((line, li) => (
        <React.Fragment key={li}>
          {line.split(URL_PATTERN).map((part, ui) => (
            ui % 2 === 1 ? (
              <a
                key={`${li}-${ui}`}
                href={part}
                target="_blank"
                rel="noopener noreferrer"
                className="break-all underline underline-offset-2"
              >
                {part}
              </a>
            ) : formatted(part, `${li}-${ui}`)
          ))}
          {li < lines.length - 1 && <br />}
        </React.Fragment>
      ))}
    </>
  );
}

/** <audio> for a case's voice note or a chat message's voice note. Renders nothing without audio. */
export function VoiceNotePlayer({ caseId, messageId = null, hasAudio, compact = false }) {
  const { status, url } = useAudioUrl({ caseId, messageId, hasAudio });
  if (status === 'none') return null;
  if (status === 'loading') {
    return (
      <span className="mt-1 flex items-center gap-1.5 text-xs text-slate-500">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading voice note...
      </span>
    );
  }
  if (status === 'error') {
    return <span className="mt-1 block text-xs italic text-slate-500">Voice note unavailable</span>;
  }
  return (
    <audio
      controls
      preload="none"
      src={url}
      className={compact ? 'mt-1.5 h-8 w-56 max-w-full' : 'mt-2 h-10 w-full'}
    >
      Your browser cannot play this voice note.
    </audio>
  );
}

/** A photo (IMAGE-1): the patient's photo on the case, or one in a chat message (the patient's or
 * the doctor's). Click opens it full size. Renders nothing when there is no photo. */
export function PhotoAttachment({ caseId, messageId = null, hasImage, compact = false }) {
  const { status, url } = useMediaUrl({ caseId, messageId, has: hasImage, kind: 'image' });
  if (status === 'none') return null;
  if (status === 'loading') {
    return (
      <span className="mt-1 flex items-center gap-1.5 text-xs text-slate-500">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading photo...
      </span>
    );
  }
  if (status === 'error') {
    return <span className="mt-1 block text-xs italic text-slate-500">Photo unavailable</span>;
  }
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" title="Open the full-size photo" className="mt-1.5 block w-fit">
      <img
        src={url}
        alt="Photo sent in the conversation"
        loading="lazy"
        className={`rounded-lg bg-slate-100 object-contain ring-1 ring-black/10 ${
          compact ? 'max-h-56 max-w-full' : 'max-h-80 max-w-full'
        }`}
      />
    </a>
  );
}

/** What the image analysis agent saw in the patient's photo (IMAGE-1). */
export function ImageAnalysisPanel({ caseData }) {
  const a = caseData?.image_analysis;
  if (!a) return null;
  if (!a.ok) {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        <span>The photo could not be analysed automatically ({a.error || 'unknown error'}). Please look at it.</span>
      </div>
    );
  }
  const quality = a.image_quality && a.image_quality !== 'clear' ? a.image_quality : null;
  return (
    <div className="rounded-lg border border-accent-200 bg-accent-50/40 p-4 text-sm">
      <span className="mb-2 flex flex-wrap items-center gap-1.5 font-medium text-ink">
        <ScanEye className="h-4 w-4 text-accent-600" /> AI photo summary
        <span className="text-xs font-normal text-slate-500">Image analysis agent, check the photo yourself</span>
      </span>
      {a.clinical_summary && <p className="font-medium leading-relaxed text-ink">{a.clinical_summary}</p>}
      {a.description && <p className="mt-1 leading-relaxed text-slate-700">{a.description}</p>}
      {(a.red_flags || []).length > 0 && (
        <ul className="mt-2 space-y-1">
          {a.red_flags.map((flag, i) => (
            <li key={i} className="flex items-start gap-1.5 font-medium text-red-700">
              <OctagonAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {flag}
            </li>
          ))}
        </ul>
      )}
      {(a.findings || []).length > 0 && (
        <ul className="mt-2 list-disc space-y-0.5 pl-5 leading-relaxed text-slate-700 marker:text-accent-400">
          {a.findings.map((f, i) => <li key={i}>{f}</li>)}
        </ul>
      )}
      {(quality || a.engine) && (
        <p className="mt-2 text-xs text-slate-500">
          {quality && <>Image quality: {quality}. </>}
          {a.engine && <span className="font-mono">{a.engine}</span>}
        </p>
      )}
    </div>
  );
}

/** needs_human_review / degraded / engine badges. */
export function CaseFlags({ caseData, showEngine = false }) {
  if (!caseData) return null;
  return (
    <>
      {caseData.needs_human_review && (
        <Badge tone="amber" icon={AlertTriangle}>Needs human review</Badge>
      )}
      {caseData.degraded && (
        <Badge
          tone="orange"
          icon={Cpu}
          title="Both Bedrock models failed; the offline keyword classifier decided this case"
        >
          Degraded triage
        </Badge>
      )}
      {showEngine && caseData.triage_engine && (
        <Badge tone="gray" className="font-mono">{caseData.triage_engine}</Badge>
      )}
    </>
  );
}

const TOOL_LABELS = {
  // Removed tool; still in the audit trail of older cases.
  dispatch_ambulance: 'Ambulance dispatch (legacy)',
  dispatch_ambulance_tool: 'Ambulance dispatch (legacy)',
  alert_emergency_contact: 'Emergency contact call',
  alert_emergency_contact_tool: 'Emergency contact call',
  call_emergency_contact: 'Emergency contact call',
  match_specialist_doctor: 'Specialist match',
  match_specialist_doctor_tool: 'Specialist match',
  generate_video_session: 'Video session',
  generate_video_session_tool: 'Video session',
};
const TOOL_META_KEYS = new Set(['tool', 'name', 'status', 'started_at', 'finished_at']);

function toolStatusStyle(status) {
  const s = String(status || '').toUpperCase();
  if (/(FAIL|ERROR|REFUSED|DENIED)/.test(s)) return { Icon: XCircle, cls: 'text-red-600' };
  if (/(SKIP|NO_|NONE|UNAVAILABLE)/.test(s)) return { Icon: MinusCircle, cls: 'text-slate-400' };
  return { Icon: CheckCircle2, cls: 'text-brand-600' };
}

function formatValue(v) {
  if (v == null) return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

function ToolResults({ results }) {
  if (!Array.isArray(results) || results.length === 0) {
    return <span className="text-sm italic text-slate-500">No tools were called for this case.</span>;
  }
  return (
    <div className="space-y-2">
      {results.map((r, idx) => {
        const name = r.tool || r.name || `tool ${idx + 1}`;
        const { Icon, cls } = toolStatusStyle(r.status);
        let duration = null;
        if (r.started_at && r.finished_at) {
          const ms = new Date(r.finished_at) - new Date(r.started_at);
          if (Number.isFinite(ms) && ms >= 0) duration = ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
        }
        const details = Object.entries(r).filter(([k]) => !TOOL_META_KEYS.has(k));
        return (
          <div key={`${name}-${idx}`} className="rounded-lg border border-slate-200 bg-slate-50/60 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5 text-sm font-medium text-slate-800">
                <Icon className={`h-4 w-4 ${cls}`} />
                {TOOL_LABELS[name] || name}
              </span>
              <span className="font-mono text-[11px] text-slate-500">
                {r.status || 'n/a'}{duration ? ` · ${duration}` : ''}
              </span>
            </div>
            {details.length > 0 && (
              <dl className="mt-2 grid grid-cols-[auto,1fr] gap-x-3 gap-y-0.5 text-[11px]">
                {details.map(([k, v]) => (
                  <React.Fragment key={k}>
                    <dt className="text-slate-500">{k}</dt>
                    <dd className="break-all font-mono text-slate-700">{formatValue(v)}</dd>
                  </React.Fragment>
                ))}
              </dl>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Facts the triage agent recalled from AgentCore Memory for this patient (MEM-1). */
export function PatientHistory({ caseData }) {
  const facts = caseData?.patient_history || [];
  if (facts.length === 0) return null;
  return (
    <div className="rounded-lg border border-brand-200 bg-brand-50/40 p-4 text-sm">
      <span className="mb-2 flex flex-wrap items-center gap-1.5 font-medium text-ink">
        <History className="h-4 w-4 text-brand-600" /> Patient history
        <span className="text-xs font-normal text-slate-500">Remembered from earlier cases, unverified</span>
      </span>
      <ul className="list-disc space-y-1 pl-5 leading-relaxed text-slate-700 marker:text-brand-400">
        {facts.map((fact, i) => <li key={i}>{fact}</li>)}
      </ul>
    </div>
  );
}

function DetailRow({ label, children }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className="min-w-0 truncate text-right text-ink">{children}</dd>
    </div>
  );
}

/** Agent audit trail (DATA-2): summary, reasoning, confidence, engine, IDs, tool results. */
export function TriageAuditPanel({ caseData, showToolResults = true, showHistory = true }) {
  if (!caseData) return null;
  const confidence =
    typeof caseData.confidence_score === 'number'
      ? `${Math.round(caseData.confidence_score <= 1 ? caseData.confidence_score * 100 : caseData.confidence_score)}%`
      : 'n/a';
  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-slate-200 p-4 text-sm">
        <span className="flex items-center gap-1.5 font-medium text-ink">
          <ClipboardList className="h-4 w-4 text-brand-600" /> AI triage classification
        </span>
        <dl className="mt-2 divide-y divide-slate-100">
          <DetailRow label="Severity">
            <span className={`font-semibold ${esiStyle(caseData.esi_level).text}`}>
              ESI {caseData.esi_level}{caseData.severity ? ` · ${caseData.severity}` : ''}
            </span>
          </DetailRow>
          <DetailRow label="Status"><StatusBadge status={caseData.status} /></DetailRow>
          <DetailRow label="Matched specialty">{caseData.matched_specialty || 'None'}</DetailRow>
          <DetailRow label="Confidence"><span className="font-mono tabular-nums">{confidence}</span></DetailRow>
          <DetailRow label="Triage engine"><span className="font-mono text-xs">{caseData.triage_engine || 'n/a'}</span></DetailRow>
          <DetailRow label="Telephony call ID"><span className="font-mono text-xs">{caseData.contact_call_id || 'None'}</span></DetailRow>
          <DetailRow label="Patient location">
            {caseData.location_latitude != null && caseData.location_longitude != null ? (
              <a
                href={`https://maps.google.com/?q=${Number(caseData.location_latitude).toFixed(6)},${Number(caseData.location_longitude).toFixed(6)}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs font-medium text-brand-700 hover:text-brand-800"
              >
                {[caseData.location_name, caseData.location_address].filter(Boolean).join(', ') || 'Open map'}
              </a>
            ) : (
              <span className="text-xs text-slate-500">{caseData.location_requested_at ? 'Requested, not shared yet' : 'Not requested'}</span>
            )}
          </DetailRow>
          {caseData.location_alert_status && (
            <DetailRow label="Location to contact"><span className="font-mono text-xs">{caseData.location_alert_status}</span></DetailRow>
          )}
          <DetailRow label="Ambulance called">
            <span className="text-xs text-slate-600">
              {caseData.ambulance_called_at
                ? `${new Date(caseData.ambulance_called_at).toLocaleString()}${caseData.ambulance_called_by ? ` by ${caseData.ambulance_called_by}` : ''}`
                : 'Not recorded'}
            </span>
          </DetailRow>
          {caseData.received_at && (
            <DetailRow label="Received"><span className="text-xs text-slate-600">{new Date(caseData.received_at).toLocaleString()}</span></DetailRow>
          )}
          {caseData.dispatched_at && (
            <DetailRow label="Emergency protocol run"><span className="text-xs text-slate-600">{new Date(caseData.dispatched_at).toLocaleString()}</span></DetailRow>
          )}
          {caseData.resolved_at && (
            <DetailRow label="Resolved"><span className="text-xs text-slate-600">{new Date(caseData.resolved_at).toLocaleString()}</span></DetailRow>
          )}
        </dl>
        {(caseData.needs_human_review || caseData.degraded) && (
          <div className="flex flex-wrap gap-1.5 pt-2">
            <CaseFlags caseData={caseData} />
          </div>
        )}
      </div>

      {showHistory && <PatientHistory caseData={caseData} />}

      {(caseData.clinical_summary || caseData.reasoning) && (
        <div className="space-y-4 rounded-lg border border-slate-200 p-4 text-sm">
          {caseData.clinical_summary && (
            <div>
              <span className="mb-1 block font-medium text-ink">Clinical summary</span>
              <p className="leading-relaxed text-slate-700">{caseData.clinical_summary}</p>
            </div>
          )}
          {caseData.reasoning && (
            <div>
              <span className="mb-1 flex items-center gap-1.5 font-medium text-ink">
                <Brain className="h-4 w-4 text-brand-600" /> Agent reasoning
              </span>
              <p className="whitespace-pre-wrap leading-relaxed text-slate-600">{caseData.reasoning}</p>
            </div>
          )}
        </div>
      )}

      {showToolResults && (
        <div>
          <span className="mb-2 flex items-center gap-1.5 text-sm font-medium text-ink">
            <Wrench className="h-4 w-4 text-slate-500" /> Tool execution results
          </span>
          <ToolResults results={caseData.tool_results} />
        </div>
      )}
    </div>
  );
}

// ==========================================
// Case history tab
// ==========================================
const OPEN_STATUSES = ['ACTIVE', 'DOCTOR_HANDOVER'];

/** Administrators delete a closed case (with its chat, media and analytics); open cases are closed first. */
function DeleteCase({ caseData, onDeleted }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const open = OPEN_STATUSES.includes(caseData.status);

  useEffect(() => { setConfirming(false); setError(''); }, [caseData.case_id]);

  const remove = async () => {
    setBusy(true);
    setError('');
    try {
      await api.deleteCase(caseData.case_id);
      onDeleted(caseData.case_id);
    } catch (err) {
      setError(err?.response?.data?.detail || 'The case could not be deleted.');
    } finally {
      setBusy(false);
    }
  };

  if (open) {
    return <p className="text-xs text-slate-500">An open case can be deleted once it is closed.</p>;
  }
  return (
    <div>
      {!confirming ? (
        <button type="button" onClick={() => setConfirming(true)} className="btn btn-danger-ghost btn-sm -ml-2.5">
          <Trash2 className="h-3.5 w-3.5" /> Delete case
        </button>
      ) : (
        <div className="notice notice-error flex-col gap-2 sm:flex-row sm:items-center">
          <span className="flex-1">
            Delete this case for good? Its chat, voice notes and photos are removed, and it leaves the analytics.
          </span>
          <span className="flex shrink-0 items-center gap-2">
            <button type="button" onClick={remove} disabled={busy} className="btn btn-danger btn-sm">
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
              Delete
            </button>
            <button type="button" aria-label="Cancel" onClick={() => setConfirming(false)} className="btn btn-ghost btn-sm !p-1.5">
              <X className="h-4 w-4" />
            </button>
          </span>
        </div>
      )}
      {error && <p role="alert" className="mt-1.5 text-xs text-red-700">{error}</p>}
    </div>
  );
}

export default function AuditLogs({ caseEvent, isAdmin = false }) {
  const [cases, setCases] = useState([]);
  const [searchTerm, setSearchTerm] = useState('');
  const [selectedCase, setSelectedCase] = useState(null);
  const [messages, setMessages] = useState([]);

  const handleDeleted = (caseId) => {
    const rest = cases.filter((c) => c.case_id !== caseId);
    setCases(rest);
    setSelectedCase(rest[0] || null);
    setMessages([]);
    if (rest[0]) loadMessages(rest[0].case_id);
  };

  useEffect(() => {
    loadAllCases();
  }, []);

  // Live case updates from /ws/cases (BRIDGE-1): upsert in place.
  useEffect(() => {
    if (!caseEvent?.case_id) return;
    setCases((prev) => {
      const idx = prev.findIndex((c) => c.case_id === caseEvent.case_id);
      if (idx === -1) return [caseEvent, ...prev];
      const next = [...prev];
      next[idx] = { ...prev[idx], ...caseEvent };
      return next;
    });
    setSelectedCase((prev) => (prev?.case_id === caseEvent.case_id ? { ...prev, ...caseEvent } : prev));
  }, [caseEvent]);

  const loadAllCases = async () => {
    try {
      const data = await api.getAllCases();
      setCases(data);
      if (data.length > 0) {
        setSelectedCase(data[0]);
        loadMessages(data[0].case_id);
      }
    } catch (err) {
      console.error("Failed to load audit cases", err);
    }
  };

  const loadMessages = async (caseId) => {
    try {
      const msgs = await api.getCaseMessages(caseId);
      setMessages(msgs);
    } catch (err) {
      console.error("Failed to load messages", err);
    }
  };

  const handleSelectCase = (c) => {
    setSelectedCase(c);
    loadMessages(c.case_id);
  };

  const term = searchTerm.toLowerCase();
  const filtered = cases.filter((c) =>
    (c.patient_name || '').toLowerCase().includes(term) ||
    (c.transcript || '').toLowerCase().includes(term) ||
    (c.clinical_summary || '').toLowerCase().includes(term) ||
    (c.assigned_doctor_name || '').toLowerCase().includes(term) ||
    (c.matched_specialty && c.matched_specialty.toLowerCase().includes(term))
  );

  return (
    <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">

      <PageHeader
        title="Case History"
        description="Searchable audit log of every voice note, agent decision, tool call and doctor–patient conversation."
        actions={
          <div className="relative w-full sm:w-80">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              type="text"
              placeholder="Search patient, symptoms, doctor..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="input pl-9"
            />
          </div>
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">

        {/* Case list (7 cols) */}
        <div className="card overflow-hidden lg:col-span-7">
          <div className="card-header">
            <h2 className="card-title">Case records</h2>
            <span className="text-xs tabular-nums text-slate-500">
              {filtered.length}{filtered.length !== cases.length ? ` of ${cases.length}` : ''} cases
            </span>
          </div>

          {filtered.length === 0 ? (
            <EmptyState
              icon={FileSearch}
              title={cases.length === 0 ? 'No cases yet' : 'No matching cases'}
              description={cases.length === 0 ? 'Cases appear here once patients send a WhatsApp message.' : 'Try a different search term.'}
            />
          ) : (
            <ul className="max-h-[820px] divide-y divide-slate-100 overflow-y-auto">
              {filtered.map((c) => {
                const isSelected = selectedCase?.case_id === c.case_id;
                return (
                  <li key={c.case_id}>
                    <button
                      type="button"
                      onClick={() => handleSelectCase(c)}
                      className={`w-full border-l-2 px-5 py-4 text-left transition-colors ${
                        isSelected ? 'border-l-brand-600 bg-brand-50/60' : 'border-l-transparent hover:bg-canvas'
                      }`}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="flex min-w-0 flex-wrap items-center gap-2">
                          <span className="truncate text-sm font-semibold text-brand-900">{c.patient_name}</span>
                          <EsiBadge level={c.esi_level} />
                          {c.status === 'RESOLVED' && <StatusBadge status="RESOLVED" />}
                          <CaseFlags caseData={c} />
                        </div>
                        <span className="shrink-0 text-xs tabular-nums text-slate-500">{formatDateTime(c.created_at)}</span>
                      </div>

                      <p className="mt-1.5 line-clamp-1 text-sm text-slate-600">{c.transcript}</p>

                      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
                        <span className="font-mono">{c.case_id}</span>
                        {c.matched_specialty && <span>{c.matched_specialty}</span>}
                        <span>Doctor: <span className="text-slate-700">{c.assigned_doctor_name || 'Unassigned'}</span></span>
                        {c.detected_language && <span className="ml-auto">{c.detected_language}</span>}
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Case detail (5 cols) */}
        <div className="lg:col-span-5">
          {selectedCase ? (
            <div className="card divide-y divide-slate-200">
              <div className="px-5 py-4">
                <span className="eyebrow">Case review · <span className="font-mono normal-case tracking-normal">{selectedCase.case_id}</span></span>
                <div className="mt-1 flex flex-wrap items-center gap-2">
                  <h2 className="text-lg font-semibold text-brand-900">{selectedCase.patient_name}</h2>
                  <EsiBadge level={selectedCase.esi_level} withName />
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                  <div>
                    <dt className="text-xs text-slate-500">Phone</dt>
                    <dd className="font-mono text-xs text-slate-800">{selectedCase.user_phone}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-slate-500">Emergency contact</dt>
                    <dd className="font-mono text-xs text-slate-800">{selectedCase.emergency_contact_phone || 'Not registered'}</dd>
                  </div>
                  <div className="col-span-2">
                    <dt className="text-xs text-slate-500">Assigned doctor</dt>
                    <dd className="text-slate-800">{selectedCase.assigned_doctor_name || 'Unassigned'}</dd>
                  </div>
                </dl>
              </div>

              {/* Voice Transcript, or the patient's photo (IMAGE-1) */}
              <div className="px-5 py-4">
                <span className="mb-1 flex items-center gap-1.5 text-sm font-medium text-ink">
                  {selectedCase.image_s3_key && !selectedCase.audio_s3_key
                    ? <><ImageIcon className="h-4 w-4 text-slate-500" /> Patient's photo</>
                    : <><Volume2 className="h-4 w-4 text-slate-500" /> Original voice note</>}
                  {selectedCase.detected_language && <Badge tone="gray" className="ml-1">{selectedCase.detected_language}</Badge>}
                </span>
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
                <blockquote className="mt-2 whitespace-pre-line border-l-2 border-slate-300 pl-3 text-sm leading-relaxed text-slate-700">
                  {selectedCase.transcript}
                </blockquote>
              </div>

              {/* Triage decision + agent audit trail */}
              <div className="px-5 py-4">
                <TriageAuditPanel caseData={selectedCase} />
              </div>

              {/* Conversation Log */}
              <div className="px-5 py-4">
                <span className="mb-2 flex items-center gap-1.5 text-sm font-medium text-ink">
                  <MessageSquare className="h-4 w-4 text-slate-500" />
                  Conversation log
                  <span className="text-xs font-normal tabular-nums text-slate-500">({messages.length})</span>
                </span>
                <div className="max-h-64 space-y-3 overflow-y-auto rounded-lg bg-canvas p-3">
                  {messages.length === 0 ? (
                    <span className="text-sm italic text-slate-500">No additional messages exchanged.</span>
                  ) : (
                    messages.map((m) => (
                      <div key={m.message_id} className="text-sm">
                        <span className={`text-xs font-semibold ${
                          m.sender === "DOCTOR" ? "text-brand-700" : m.sender === "BOT" ? "text-slate-500" : "text-ink"
                        }`}>
                          {m.sender_name}{m.sender === "BOT" ? " (automated)" : ""}
                        </span>
                        <p className={m.sender === "BOT" ? "text-slate-500" : "text-slate-700"}>
                          <WhatsAppText text={m.content} />
                        </p>
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
                    ))
                  )}
                </div>
              </div>

              {isAdmin && (
                <div className="px-5 py-3">
                  <DeleteCase caseData={selectedCase} onDeleted={handleDeleted} />
                </div>
              )}
            </div>
          ) : (
            <div className="card">
              <EmptyState icon={History} title="No case selected" description="Select a case to inspect its clinical log." />
            </div>
          )}
        </div>

      </div>
    </div>
  );
}
