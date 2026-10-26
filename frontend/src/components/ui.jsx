import React from 'react';
import { Activity, X } from 'lucide-react';

// ==========================================
// Shared presentational primitives (no data fetching)
// ==========================================

/** ESI 1-5 severity scale. These colors are reserved for severity and never reused for decoration. */
export const ESI_STYLES = {
  1: { name: 'Resuscitation', badge: 'bg-red-50 text-red-700 ring-red-600/20', dot: 'bg-red-600', stripe: 'border-l-red-600', text: 'text-red-700' },
  2: { name: 'Emergent', badge: 'bg-orange-50 text-orange-700 ring-orange-600/20', dot: 'bg-orange-500', stripe: 'border-l-orange-500', text: 'text-orange-700' },
  3: { name: 'Urgent', badge: 'bg-amber-50 text-amber-800 ring-amber-600/25', dot: 'bg-amber-400', stripe: 'border-l-amber-400', text: 'text-amber-800' },
  4: { name: 'Less urgent', badge: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20', dot: 'bg-emerald-500', stripe: 'border-l-emerald-500', text: 'text-emerald-700' },
  5: { name: 'Non-urgent', badge: 'bg-sky-50 text-sky-700 ring-sky-600/20', dot: 'bg-sky-500', stripe: 'border-l-sky-500', text: 'text-sky-700' },
};
const ESI_UNKNOWN = { name: 'Unknown', badge: 'bg-slate-50 text-slate-600 ring-slate-500/20', dot: 'bg-slate-400', stripe: 'border-l-slate-300', text: 'text-slate-600' };

export function esiStyle(level) {
  return ESI_STYLES[Number(level)] || ESI_UNKNOWN;
}

export function EsiBadge({ level, withName = false, className = '' }) {
  const s = esiStyle(level);
  return (
    <span className={`badge font-semibold ${s.badge} ${className}`} title={`ESI ${level ?? '?'}: ${s.name}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${s.dot} ${Number(level) === 1 ? 'animate-pulse' : ''}`} />
      ESI {level ?? '?'}
      {withName && <span className="font-medium opacity-80">· {s.name}</span>}
    </span>
  );
}

// Badge tones. SUDO navy / bright blue carry information; green, amber and red only state
// (done, needs attention, critical). `orange` and `violet` are kept as aliases so callers never
// add a new hue.
const TONES = {
  gray: 'bg-slate-50 text-slate-600 ring-slate-500/20',
  brand: 'bg-brand-50 text-brand-700 ring-brand-600/20',
  accent: 'bg-accent-50 text-accent-700 ring-accent-600/20',
  green: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  amber: 'bg-amber-50 text-amber-800 ring-amber-600/25',
  red: 'bg-red-50 text-red-700 ring-red-600/20',
};
TONES.orange = TONES.amber;
TONES.violet = TONES.brand;

export function Badge({ tone = 'gray', icon: Icon, className = '', children, ...rest }) {
  return (
    <span className={`badge ${TONES[tone] || TONES.gray} ${className}`} {...rest}>
      {Icon && <Icon className="h-3 w-3" />}
      {children}
    </span>
  );
}

const STATUS_TONES = {
  ACTIVE: ['accent', 'Active'],
  DOCTOR_HANDOVER: ['brand', 'Doctor handover'],
  RESOLVED: ['green', 'Resolved'],
  DISMISSED: ['gray', 'Dismissed'],
  PENDING: ['amber', 'Pending'],
};

export function StatusBadge({ status }) {
  if (!status) return null;
  const [tone, label] = STATUS_TONES[status] || ['gray', String(status).replace(/_/g, ' ').toLowerCase()];
  return <Badge tone={tone} className="capitalize">{label}</Badge>;
}

/** Small live/polling indicator. */
export function LiveIndicator({ live, liveLabel = 'Live', offLabel = 'Polling', title, onDark = false }) {
  const color = live ? (onDark ? 'text-accent-200' : 'text-accent-700') : (onDark ? 'text-white/60' : 'text-slate-500');
  return (
    <span title={title} className={`inline-flex items-center gap-1.5 text-xs font-medium ${color}`}>
      <span className="relative flex h-2 w-2">
        {live && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent-400 opacity-60" />}
        <span className={`relative inline-flex h-2 w-2 rounded-full ${live ? 'bg-accent-400' : 'bg-slate-400'}`} />
      </span>
      {live ? liveLabel : offLabel}
    </span>
  );
}

export function PageHeader({ title, description, actions, children }) {
  return (
    <div className="mb-6 flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
      <div className="min-w-0">
        <h1 className="flex flex-wrap items-center gap-2.5 font-display text-2xl font-semibold tracking-tight text-brand-900">
          {title}
          {children}
        </h1>
        {description && <p className="mt-1 max-w-3xl text-sm text-slate-500">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function EmptyState({ icon: Icon, title, description, className = '' }) {
  return (
    <div className={`flex flex-col items-center justify-center px-6 py-12 text-center ${className}`}>
      {Icon && (
        <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-full bg-brand-50 text-brand-400">
          <Icon className="h-5 w-5" />
        </div>
      )}
      <p className="text-sm font-medium text-slate-900">{title}</p>
      {description && <p className="mt-1 max-w-xs text-sm text-slate-500">{description}</p>}
    </div>
  );
}

export function initials(name, fallback = '?') {
  if (!name) return fallback;
  const parts = String(name).replace(/^Dr\.?\s+/i, '').split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || fallback;
}

export function Avatar({ name, className = 'h-9 w-9 text-xs', tone = 'bg-brand-50 text-brand-700 ring-brand-600/10' }) {
  return (
    <span className={`inline-flex shrink-0 items-center justify-center rounded-full font-semibold ring-1 ring-inset ${tone} ${className}`}>
      {initials(name)}
    </span>
  );
}

export function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function formatDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${formatTime(iso)}`;
}

/** "5m ago" style relative time for queue items. */
export function timeAgo(iso) {
  if (!iso) return 'Now';
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return '';
  const min = Math.round(ms / 60000);
  if (min < 1) return 'Just now';
  if (min < 60) return `${min}m ago`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h}h ago`;
  return formatDateTime(iso);
}

/** On / off switch (SUDO navy when on). */
export function Switch({ checked, onChange, label, disabled = false, className = '' }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={Boolean(checked)}
      aria-label={label}
      disabled={disabled}
      onClick={onChange}
      className={`switch ${checked ? 'switch-on' : 'switch-off'} ${className}`}
    >
      <span className={`switch-knob ${checked ? 'translate-x-[22px]' : 'translate-x-0.5'}`} />
    </button>
  );
}

/** The ResQ-AI product mark on the SUDO gradient. */
export function BrandMark({ className = 'h-8 w-8', iconClassName = 'h-[18px] w-[18px]' }) {
  return (
    <div className={`flex shrink-0 items-center justify-center rounded-lg bg-brand-gradient shadow-sm ${className}`}>
      <Activity className={`${iconClassName} text-white`} strokeWidth={2.5} />
    </div>
  );
}

/** Modal shell: overlay + white panel with a header. */
export function Modal({ title, subtitle, icon: Icon, onClose, headerExtra, size = 'max-w-2xl', children }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-brand-950/50 p-2 backdrop-blur-sm sm:p-4">
      <div className={`flex max-h-full w-full ${size} flex-col overflow-hidden rounded-2xl bg-white shadow-2xl ring-1 ring-slate-900/5`}>
        <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
          <div className="flex min-w-0 items-center gap-3">
            {Icon && (
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
                <Icon className="h-5 w-5" />
              </div>
            )}
            <div className="min-w-0">
              <h2 className="truncate font-display text-base font-semibold text-brand-900">{title}</h2>
              {subtitle && <p className="truncate text-xs text-slate-500">{subtitle}</p>}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {headerExtra}
            <button onClick={onClose} aria-label="Close" className="btn btn-ghost btn-sm !p-1.5">
              <X className="h-5 w-5" />
            </button>
          </div>
        </div>
        {children}
      </div>
    </div>
  );
}
