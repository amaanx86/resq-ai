import React, { useState, useEffect } from 'react';
import {
  Users, Activity, ShieldCheck, Clock, TrendingUp, HeartPulse,
  Brain, Bone, Wind, Globe, Stethoscope, FlaskConical, Loader2
} from 'lucide-react';
import { api } from '../services/api';
import { Badge, PageHeader, EmptyState } from './ui';

export default function AnalyticsDashboard() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [month, setMonth] = useState(''); // '' = backend default (current UTC month)

  useEffect(() => {
    loadAnalytics(month);
  }, [month]);

  const loadAnalytics = async (selectedMonth) => {
    setLoading(true);
    try {
      const summary = await api.getAnalytics(selectedMonth || undefined);
      setData(summary);
      setError(null);
    } catch (err) {
      console.error("Failed to load analytics", err);
      setError(err?.response?.status === 403 ? 'Analytics are restricted to administrators.' : 'Analytics could not be loaded.');
    } finally {
      setLoading(false);
    }
  };

  if (!loading && !data) {
    return (
      <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <div className="card">
          <EmptyState icon={TrendingUp} title="Analytics unavailable" description={error || 'No analytics available.'} />
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex flex-col items-center justify-center py-32 text-slate-500">
        <Loader2 className="mb-3 h-6 w-6 animate-spin text-brand-600" />
        <p className="text-sm">Loading analytics...</p>
      </div>
    );
  }

  const urgentCases = data.total_urgent_cases ?? 0;
  const avgResponse = typeof data.avg_response_time_seconds === 'number'
    ? `${data.avg_response_time_seconds.toFixed(1)}s`
    : 'n/a';
  const avgAccept = typeof data.avg_time_to_accept_seconds === 'number'
    ? `${data.avg_time_to_accept_seconds.toFixed(0)}s`
    : 'n/a';
  const monthlyCalls = data.monthly_doctor_calls || {};
  const bySpecialty = data.cases_by_specialty || {};
  const languages = data.languages_handled || {};
  const coveredMonth = data.month || month || new Date().toISOString().slice(0, 7);
  const monthLabel = (() => {
    const [y, m] = String(coveredMonth).split('-').map(Number);
    if (!y || !m) return coveredMonth;
    return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString(undefined, { month: 'long', year: 'numeric', timeZone: 'UTC' });
  })();
  const languageTotal = Object.values(languages).reduce((a, b) => a + (Number(b) || 0), 0);

  return (
    <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">

      <PageHeader
        title="Analytics"
        description="Patient onboarding, triage outcomes and doctor handovers for the selected month."
        actions={
          <label className="flex items-center gap-2 text-sm text-slate-500">
            <span className="hidden sm:inline">Reporting month</span>
            <input
              type="month"
              value={month || coveredMonth}
              max={new Date().toISOString().slice(0, 7)}
              onChange={(e) => setMonth(e.target.value)}
              className="input w-auto py-1.5"
            />
            {loading && <Loader2 className="h-4 w-4 animate-spin text-brand-600" />}
          </label>
        }
      >
        {data.demo_mode && (
          <Badge
            tone="amber"
            icon={FlaskConical}
            title="DEMO_MODE is on: figures include demo padding and are not real operational data"
          >
            Demo data
          </Badge>
        )}
      </PageHeader>

      {error && <p className="-mt-3 mb-6 text-sm text-red-600">{error}</p>}

      {/* KPI tiles */}
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-6">
        <StatTile
          label="Registered patients"
          value={data.total_registered_users}
          note="WhatsApp profiles on record"
          icon={Users}
          chip="bg-brand-50 text-brand-600"
        />
        <StatTile
          label="Critical (ESI 1–2)"
          value={data.total_emergency_cases}
          note="Contact called + specialist handover"
          icon={Activity}
          chip="bg-red-50 text-red-600"
        />
        <StatTile
          label="Urgent (ESI 3)"
          value={urgentCases}
          note="Doctor review, no emergency protocol"
          icon={Stethoscope}
          chip="bg-amber-50 text-amber-600"
        />
        <StatTile
          label="Non-urgent (ESI 4–5)"
          value={data.total_false_alarms}
          note="Self-care advice only"
          icon={ShieldCheck}
          chip="bg-emerald-50 text-emerald-600"
        />
        <StatTile
          label="Avg. triage latency"
          value={avgResponse}
          note={avgResponse === 'n/a' ? 'No ESI 1–2 cases with timings yet' : <>Message received &rarr; contact called + doctors offered</>}
          icon={Clock}
          chip="bg-slate-100 text-slate-600"
        />
        <StatTile
          label="Avg. time to accept"
          value={avgAccept}
          note={avgAccept === 'n/a' ? 'No accepted case offers yet' : <>Case offered &rarr; a doctor accepted</>}
          icon={Stethoscope}
          chip="bg-brand-50 text-brand-600"
        />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">

        {/* Monthly doctor handovers */}
        <div className="card">
          <div className="card-header">
            <h2 className="card-title">Doctor handovers by physician</h2>
            <span className="text-xs text-slate-500">{monthLabel}</span>
          </div>
          <div className="px-5 py-5">
            <BarList data={monthlyCalls} unit="calls" emptyText="No doctor handovers this month yet." />
          </div>
        </div>

        {/* Cases by specialty + languages */}
        <div className="card">
          <div className="card-header">
            <h2 className="card-title">Emergencies by department</h2>
            <span className="text-xs text-slate-500">AI triage classification</span>
          </div>
          <div className="px-5 py-5">
            <BarList
              data={bySpecialty}
              unit="cases"
              emptyText="No emergencies recorded yet."
              iconFor={(name) => SPECIALTY_ICONS[name] || HeartPulse}
            />
          </div>

          <div className="border-t border-slate-200 px-5 py-5">
            <h3 className="mb-3 flex items-center gap-1.5 text-sm font-medium text-ink">
              <Globe className="h-4 w-4 text-slate-500" />
              Voice notes by language
              <span className="text-xs font-normal text-slate-500">(Amazon Transcribe)</span>
            </h3>
            {Object.keys(languages).length === 0 ? (
              <p className="text-sm italic text-slate-500">No voice notes yet.</p>
            ) : (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                {Object.entries(languages).sort((a, b) => b[1] - a[1]).map(([lang, count]) => (
                  <div key={lang} className="rounded-lg border border-slate-200 px-3 py-2.5">
                    <span className="block text-xs text-slate-500">{lang}</span>
                    <span className="text-lg font-semibold tabular-nums text-ink">{count}</span>
                    {languageTotal > 0 && (
                      <span className="ml-1.5 text-xs tabular-nums text-slate-400">{Math.round((count / languageTotal) * 100)}%</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

      </div>
    </div>
  );
}

const SPECIALTY_ICONS = {
  Cardiology: HeartPulse,
  Neurology: Brain,
  Trauma: Bone,
  Pulmonology: Wind,
};

function StatTile({ label, value, note, icon: Icon, chip }) {
  return (
    <div className="card px-5 py-4">
      <div className="flex items-start justify-between gap-3">
        <span className="text-sm font-medium text-slate-500">{label}</span>
        <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${chip}`}>
          <Icon className="h-4 w-4" />
        </div>
      </div>
      <p className="mt-1 text-3xl font-semibold tracking-tight tabular-nums text-ink">{value ?? '—'}</p>
      <p className="mt-1 text-xs text-slate-500">{note}</p>
    </div>
  );
}

/** Single-series horizontal bar list, sorted by value. */
function BarList({ data, unit, emptyText, iconFor }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return <p className="text-sm italic text-slate-500">{emptyText}</p>;
  const max = Math.max(...entries.map(([, v]) => Number(v) || 0), 1);
  return (
    <ul className="space-y-4">
      {entries.map(([name, count]) => {
        const Icon = iconFor?.(name);
        return (
          <li key={name} title={`${name}: ${count} ${unit}`}>
            <div className="mb-1.5 flex items-center justify-between gap-3 text-sm">
              <span className="flex min-w-0 items-center gap-2 text-slate-700">
                {Icon && <Icon className="h-4 w-4 shrink-0 text-slate-400" />}
                <span className="truncate">{name}</span>
              </span>
              <span className="shrink-0 tabular-nums text-ink">
                <span className="font-semibold">{count}</span> <span className="text-xs text-slate-500">{unit}</span>
              </span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full bg-brand-500 transition-all duration-500"
                style={{ width: `${Math.max(2, Math.round((count / max) * 100))}%` }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}
