import React, { useEffect, useState } from 'react';
import { Hourglass, UserCheck, UserX } from 'lucide-react';
import { Badge } from './ui';

// ==========================================
// Doctor case offers (OFFER-1 / OFFER-2): a doctor accepts a case before it is assigned to them.
// Case fields: doctor_offer_status (PENDING | ACCEPTED | EXHAUSTED), offer_id, offered_doctor_ids,
// offer_expires_at, declined_doctor_ids, offer_history. EXHAUSTED: no free doctor took the case; it
// waits (no_doctor_since, no_doctor_reason) and is offered again as soon as a doctor is free.
// ==========================================

/** True while this case is offered to this doctor and they have not declined it. */
export function isOfferedTo(caseData, doctorId) {
  return Boolean(
    doctorId
    && caseData?.doctor_offer_status === 'PENDING'
    && (caseData.offered_doctor_ids || []).includes(doctorId)
    && !(caseData.declined_doctor_ids || []).includes(doctorId)
  );
}

/** Seconds left until an ISO time (0 when passed, null without a time), ticking every second. */
export function useCountdown(iso) {
  const left = () => (iso ? Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 1000)) : null);
  const [seconds, setSeconds] = useState(left);
  useEffect(() => {
    setSeconds(left());
    if (!iso) return undefined;
    const timer = setInterval(() => setSeconds(left()), 1000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [iso]);
  return seconds;
}

export function formatCountdown(seconds) {
  if (seconds == null) return '';
  const m = Math.floor(seconds / 60);
  const s = String(seconds % 60).padStart(2, '0');
  return `${m}:${s}`;
}

/** Offer state chip for the queue and the case header. */
export function OfferBadge({ caseData }) {
  const seconds = useCountdown(caseData?.doctor_offer_status === 'PENDING' ? caseData.offer_expires_at : null);
  const status = caseData?.doctor_offer_status;
  if (caseData?.assigned_doctor_name) {
    return <Badge tone="brand" icon={UserCheck}>{caseData.assigned_doctor_name}</Badge>;
  }
  if (status === 'PENDING') {
    const round = (caseData.offer_history || []).filter((r) => r.outcome !== 'SUPERSEDED').length;
    return (
      <Badge tone="amber" icon={Hourglass} title="Waiting for a doctor to accept">
        Offered{round > 1 ? ` (round ${round})` : ''}{seconds != null ? ` · ${formatCountdown(seconds)}` : ''}
      </Badge>
    );
  }
  if (status === 'EXHAUSTED') {
    const declined = caseData.no_doctor_reason === 'ALL_DECLINED';
    return (
      <Badge tone="red" icon={UserX}
             title="Waiting for a free doctor: offered again as soon as one is free; an administrator can assign one">
        {declined ? 'All doctors declined' : 'No doctor free'}
      </Badge>
    );
  }
  return null;
}

/** An open case no free doctor took (OFFER-5): it waits, and administrators are alerted (ADMIN-1). */
export function isWaitingForDoctor(caseData) {
  return Boolean(
    caseData
    && ['ACTIVE', 'DOCTOR_HANDOVER'].includes(caseData.status)
    && caseData.doctor_offer_status === 'EXHAUSTED'
    && !caseData.assigned_doctor_id
  );
}

/** Alarm for a case without a doctor: three urgent tones for ESI 1-2, two lower ones otherwise. */
export function playNoDoctorAlarm(esiLevel) {
  const urgent = Number(esiLevel) <= 2;
  playTones(urgent ? [0, 0.3, 0.6] : [0, 0.3], urgent ? 988 : 660);
}

/** Short beep for a new offer (Web Audio, no asset). Silently skipped if the browser blocks audio. */
export function playOfferAlert() {
  playTones([0, 0.25], 880);
}

function playTones(starts, frequency) {
  try {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    const ctx = new AudioContext();
    starts.forEach((start) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = frequency;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + start);
      gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + start + 0.2);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + start);
      osc.stop(ctx.currentTime + start + 0.22);
    });
    setTimeout(() => ctx.close(), 1000);
  } catch {
    // Autoplay policy or no audio device: the popup is still shown.
  }
}
