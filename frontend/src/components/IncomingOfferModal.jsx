import React, { useState } from 'react';
import { BellRing, Check, Loader2, X } from 'lucide-react';
import { api } from '../services/api';
import { Badge, EsiBadge, Modal } from './ui';
import { formatCountdown, useCountdown } from './offers';

// "Incoming case" popup (OFFER-2): shown to a doctor while a case is offered to them. Accept assigns
// the case if nobody else was faster; Decline passes it to the next doctor. The popup closes by
// itself when the offer ends (taken, expired, reassigned): the parent only renders it while the
// case is still offered to this doctor.
export default function IncomingOfferModal({ caseData, doctorId, onAccepted, onAnswered, onDismiss }) {
  const seconds = useCountdown(caseData.offer_expires_at);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  const answer = async (action) => {
    setBusy(action);
    setError(null);
    try {
      const call = action === 'accept' ? api.acceptOffer : api.declineOffer;
      // The backend takes the doctor from the token; doctorId only matters with auth off (local).
      const updated = await call(caseData.case_id, caseData.offer_id, doctorId);
      onAnswered?.(updated);
      if (action === 'accept') onAccepted?.(updated);
    } catch (err) {
      const status = err?.response?.status;
      setError(status === 409
        ? 'This case has already been taken or reassigned.'
        : 'Could not send your answer. Please try again.');
    } finally {
      setBusy(null);
    }
  };

  const expired = seconds === 0;
  return (
    <Modal
      title="Incoming emergency case"
      subtitle={`Case ${caseData.case_id}`}
      icon={BellRing}
      onClose={onDismiss}
      size="max-w-lg"
    >
      <div className="space-y-4 px-5 py-5">
        <div className="flex flex-wrap items-center gap-1.5">
          <EsiBadge level={caseData.esi_level} withName />
          <Badge tone="brand">{caseData.matched_specialty || 'General Emergency'}</Badge>
          {caseData.needs_human_review && <Badge tone="amber">Voice note needs listening</Badge>}
        </div>
        <div>
          <p className="text-sm font-semibold text-ink">{caseData.patient_name}</p>
          <p className="mt-1 text-sm leading-relaxed text-slate-700">
            {caseData.clinical_summary || caseData.transcript || 'No summary yet.'}
          </p>
        </div>
        <div className="flex items-center justify-between rounded-lg bg-amber-50 px-4 py-3 text-sm text-amber-900 ring-1 ring-inset ring-amber-600/20">
          <span>{expired ? 'Time is up: the case is moving to the next doctor.' : 'Answer before the time runs out, or the case goes to the next doctor.'}</span>
          <span className="ml-3 font-mono text-base font-semibold tabular-nums">{formatCountdown(seconds)}</span>
        </div>
        {error && <p className="text-sm text-red-600">{error}</p>}
      </div>
      <div className="flex justify-end gap-2 border-t border-slate-200 bg-canvas px-5 py-3.5">
        <button onClick={() => answer('decline')} disabled={Boolean(busy)} className="btn btn-secondary">
          {busy === 'decline' ? <Loader2 className="h-4 w-4 animate-spin" /> : <X className="h-4 w-4" />}
          <span>Decline</span>
        </button>
        <button onClick={() => answer('accept')} disabled={Boolean(busy) || expired} className="btn btn-primary">
          {busy === 'accept' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
          <span>Accept case</span>
        </button>
      </div>
    </Modal>
  );
}
