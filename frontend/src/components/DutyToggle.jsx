import React, { useState } from 'react';
import { Loader2 } from 'lucide-react';

function errorText(err) {
  const detail = err?.response?.data?.detail;
  return typeof detail === 'string' ? detail : 'Your availability could not be changed. Try again.';
}

/**
 * A doctor's own on/off duty switch (the navigation shows it to signed-in doctors).
 * Off duty: no new case offers; cases already assigned stay with the doctor.
 * `compact` is the mobile top-bar pill, otherwise the sidebar card.
 */
export default function DutyToggle({ onDuty, onChange, compact = false }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await onChange(!onDuty);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const label = onDuty ? 'On duty' : 'Off duty';

  if (compact) {
    return (
      <button
        type="button"
        role="switch"
        aria-checked={Boolean(onDuty)}
        aria-label="On duty"
        onClick={toggle}
        disabled={busy}
        title={error || (onDuty ? 'You receive new case offers. Tap to go off duty.' : 'You get no new case offers. Tap to go on duty.')}
        className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 transition-colors ${
          error
            ? 'bg-red-500/10 text-red-300 ring-red-400/40'
            : onDuty
              ? 'bg-white/15 text-white ring-accent-300/50'
              : 'bg-white/5 text-white/70 ring-white/20'
        }`}
      >
        {busy
          ? <Loader2 className="h-3 w-3 animate-spin" />
          : <span className={`h-2 w-2 rounded-full ${onDuty ? 'bg-accent-300' : 'bg-white/40'}`} />}
        {error ? 'Not saved' : label}
      </button>
    );
  }

  return (
    <div className="mx-3 mb-3 rounded-lg bg-white/10 px-3 py-2.5">
      <div className="flex items-center justify-between gap-3">
        <div className="leading-tight">
          <p className="text-xs text-white/60">Availability</p>
          <p className={`text-sm font-medium ${onDuty ? 'text-white' : 'text-white/70'}`}>{label}</p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={Boolean(onDuty)}
          aria-label="On duty"
          onClick={toggle}
          disabled={busy}
          className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-300 focus-visible:ring-offset-2 focus-visible:ring-offset-brand-600 disabled:opacity-60 ${
            onDuty ? 'bg-accent-500' : 'bg-white/25'
          }`}
        >
          <span className={`inline-block h-5 w-5 rounded-full bg-white shadow transition-transform ${onDuty ? 'translate-x-[22px]' : 'translate-x-0.5'}`} />
        </button>
      </div>
      <p className="mt-1.5 text-[11px] leading-snug text-white/60">
        {onDuty ? 'You receive new case offers.' : 'No new case offers. Your open cases stay with you.'}
      </p>
      {error && <p role="alert" className="mt-1.5 text-[11px] leading-snug text-red-300">{error}</p>}
    </div>
  );
}
