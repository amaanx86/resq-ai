import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BellRing, BellOff, ExternalLink, Siren } from 'lucide-react';
import { api } from '../services/api';
import { EsiBadge, timeAgo } from './ui';
import { isWaitingForDoctor, playNoDoctorAlarm } from './offers';

// ==========================================
// No-doctor alerts (ADMIN-1), administrators only: every open case that no free doctor took
// (doctor_offer_status EXHAUSTED) is listed in a red banner, with an alarm every 30 s until an
// administrator acknowledges it, and a browser notification (also when the tab is in the
// background) for each new waiting period. ESI 1-2 cases also ring the administrators' phones
// (backend, app/admin_alerts.py); Acknowledge stops those calls too.
// ==========================================

const ALARM_EVERY_MS = 30000;

function notificationsSupported() {
  return typeof window !== 'undefined' && 'Notification' in window;
}

function reasonText(c) {
  return c.no_doctor_reason === 'ALL_DECLINED' ? 'All doctors declined' : 'No doctor free';
}

function notify(c) {
  if (!notificationsSupported() || Notification.permission !== 'granted') return;
  try {
    const n = new Notification(`ESI ${c.esi_level} case has no doctor`, {
      body: `${c.patient_name}: ${reasonText(c)}. Open the ResQ-AI portal to assign a doctor.`,
      tag: `no-doctor-${c.case_id}`,
      requireInteraction: Number(c.esi_level) <= 2,
    });
    n.onclick = () => window.focus();
  } catch {
    // Some browsers only allow notifications from a service worker; the banner and alarm remain.
  }
}

export default function AdminAlerts({ cases, onOpenCase, onCaseUpdated }) {
  const waiting = useMemo(
    () => cases.filter(isWaitingForDoctor)
      .sort((a, b) => (Number(a.esi_level) - Number(b.esi_level))
        || (a.no_doctor_since || a.created_at || '').localeCompare(b.no_doctor_since || b.created_at || '')),
    [cases]
  );
  const unacknowledged = waiting.filter((c) => !c.admin_alert_ack_at);
  const [permission, setPermission] = useState(() => (notificationsSupported() ? Notification.permission : 'unsupported'));
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [, setTick] = useState(0);

  // A browser notification once per waiting period (case + no_doctor_since).
  const notified = useRef(new Set());
  useEffect(() => {
    const fresh = waiting.filter((c) => !notified.current.has(`${c.case_id}:${c.no_doctor_since}`));
    fresh.forEach((c) => notified.current.add(`${c.case_id}:${c.no_doctor_since}`));
    fresh.filter((c) => !c.admin_alert_ack_at).forEach(notify);
    if (fresh.some((c) => !c.admin_alert_ack_at)) playNoDoctorAlarm(Math.min(...fresh.map((c) => Number(c.esi_level) || 5)));
  }, [waiting]);

  // The alarm repeats while anything is unacknowledged; the waiting times tick with it.
  const worst = unacknowledged.length ? Math.min(...unacknowledged.map((c) => Number(c.esi_level) || 5)) : null;
  useEffect(() => {
    if (worst == null) return undefined;
    const timer = setInterval(() => {
      playNoDoctorAlarm(worst);
      setTick((t) => t + 1);
    }, ALARM_EVERY_MS);
    return () => clearInterval(timer);
  }, [worst]);

  const enableNotifications = useCallback(async () => {
    if (!notificationsSupported()) return;
    try {
      setPermission(await Notification.requestPermission());
    } catch {
      setPermission(Notification.permission);
    }
  }, []);

  const acknowledge = useCallback(async (c) => {
    setBusy(c.case_id);
    setError(null);
    try {
      onCaseUpdated(await api.acknowledgeNoDoctorAlert(c.case_id));
    } catch (err) {
      setError(err?.response?.data?.detail || 'Could not acknowledge the alert.');
    } finally {
      setBusy(null);
    }
  }, [onCaseUpdated]);

  if (!waiting.length) return null;

  return (
    <section className="border-b border-red-200 bg-red-50" role="alert" aria-live="assertive">
      <div className="mx-auto max-w-7xl px-4 py-3 sm:px-6 lg:px-8">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm font-semibold text-red-800">
            <Siren className="h-4 w-4 animate-pulse" />
            {waiting.length === 1 ? '1 case has no doctor' : `${waiting.length} cases have no doctor`}
            <span className="font-normal text-red-700">
              · offered again automatically as soon as a doctor is free; you can assign one now.
            </span>
          </div>
          {permission === 'default' && (
            <button type="button" onClick={enableNotifications} className="btn btn-sm btn-secondary">
              <BellRing className="h-3.5 w-3.5" /> Enable alerts
            </button>
          )}
          {permission === 'denied' && (
            <span className="flex items-center gap-1 text-xs text-red-700">
              <BellOff className="h-3.5 w-3.5" /> Browser notifications are blocked for this site.
            </span>
          )}
        </div>
        <ul className="mt-2 divide-y divide-red-100">
          {waiting.map((c) => (
            <li key={c.case_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
              <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm text-slate-800">
                <EsiBadge level={c.esi_level} />
                <span className="font-semibold">{c.patient_name}</span>
                <span className="text-slate-600">{reasonText(c)}</span>
                <span className="text-slate-500">· waiting since {timeAgo(c.no_doctor_since || c.created_at)}</span>
                {c.admin_alert_ack_at && (
                  <span className="text-xs text-slate-500">· handled by {c.admin_alert_ack_by}</span>
                )}
              </div>
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => onOpenCase(c)} className="btn btn-sm btn-primary">
                  <ExternalLink className="h-3.5 w-3.5" /> Open case
                </button>
                {!c.admin_alert_ack_at && (
                  <button type="button" onClick={() => acknowledge(c)} disabled={busy === c.case_id}
                          className="btn btn-sm btn-secondary">
                    {busy === c.case_id ? 'Saving…' : 'Acknowledge'}
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
        {error && <p className="mt-1 text-xs text-red-700">{error}</p>}
      </div>
    </section>
  );
}
