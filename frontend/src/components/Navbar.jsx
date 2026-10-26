import React from 'react';
import { LayoutDashboard, Stethoscope, ShieldCheck, BarChart3, History, Mic, LogOut } from 'lucide-react';
import { Avatar, BrandMark, LiveIndicator } from './ui';
import DutyToggle from './DutyToggle';

const TABS = [
  { id: 'console', label: 'Live Console', Icon: LayoutDashboard },
  { id: 'doctors', label: 'Doctors', Icon: Stethoscope },
  { id: 'admins', label: 'Administrators', Icon: ShieldCheck },
  { id: 'analytics', label: 'Analytics', Icon: BarChart3 },
  { id: 'logs', label: 'Case History', Icon: History },
];

function roleLabel(user) {
  if (!user) return '';
  if (user.isAdmin) return 'Administrator';
  if (user.isDoctor) return 'Doctor';
  return 'No role';
}

function Brand() {
  return (
    <div className="flex items-center gap-2.5">
      <BrandMark className="h-8 w-8 ring-1 ring-white/20" />
      <div className="leading-tight">
        <span className="block font-display text-[15px] font-semibold tracking-tight text-white">ResQ-AI</span>
        <span className="block text-[11px] font-medium text-white/60">Emergency Triage Portal</span>
      </div>
    </div>
  );
}

/**
 * Portal navigation: a fixed sidebar on large screens and a compact top bar with
 * scrollable tabs on small screens. Doctors only see the tabs in `allowedTabs`, and get their
 * on/off duty switch (`availability`: { onDuty, onChange }).
 */
export default function Navbar({
  activeTab,
  setActiveTab,
  activeCount,
  onOpenSimulator,
  allowedTabs = TABS.map((t) => t.id),
  user = null,
  authEnabled = false,
  onLogout,
  casesLive = false,
  availability = null,
}) {
  const tabs = TABS.filter((t) => allowedTabs.includes(t.id));

  const countPill = (id) =>
    id === 'console' && activeCount > 0 ? (
      <span className="ml-auto min-w-[1.25rem] rounded-full bg-white px-1.5 py-0.5 text-center text-[11px] font-semibold tabular-nums text-brand-700">
        {activeCount}
      </span>
    ) : null;

  const userBlock = authEnabled && user ? (
    <div className="flex items-center gap-3">
      <Avatar name={user.name || user.email} className="h-8 w-8 text-[11px]" tone="bg-white/15 text-white ring-white/20" />
      <div className="min-w-0 flex-1 leading-tight">
        <p className="truncate text-sm font-medium text-white" title={user.email || user.name}>{user.name}</p>
        <p className="truncate text-xs text-white/60">
          {roleLabel(user)}
          {user.doctorId && !user.isAdmin && <span className="font-mono"> · {user.doctorId}</span>}
        </p>
      </div>
      <button
        onClick={onLogout}
        title="Sign out"
        aria-label="Sign out"
        className="rounded-md p-1.5 text-white/60 transition-colors hover:bg-white/10 hover:text-white"
      >
        <LogOut className="h-4 w-4" />
      </button>
    </div>
  ) : (
    <div
      title="VITE_AUTH_ENABLED is off: no sign-in, full administrator access"
      className="flex items-center gap-3"
    >
      <Avatar name="Local Admin" className="h-8 w-8 text-[11px]" tone="bg-white/15 text-white ring-white/20" />
      <div className="leading-tight">
        <p className="text-sm font-medium text-white">Local admin</p>
        <p className="text-xs text-white/60">Authentication off</p>
      </div>
    </div>
  );

  return (
    <>
      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-64 flex-col bg-brand-600 lg:flex">
        <div className="flex h-16 items-center px-5">
          <Brand />
        </div>

        <nav className="flex-1 space-y-1 px-3 pt-4">
          <p className="px-3 pb-2 text-[11px] font-semibold uppercase tracking-wider text-white/50">Workspace</p>
          {tabs.map(({ id, label, Icon }) => {
            const active = activeTab === id;
            return (
              <button
                key={id}
                onClick={() => setActiveTab(id)}
                aria-current={active ? 'page' : undefined}
                className={`group relative flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                  active ? 'bg-white/15 text-white' : 'text-white/70 hover:bg-white/10 hover:text-white'
                }`}
              >
                {active && <span className="absolute inset-y-1.5 left-0 w-1 rounded-r bg-accent-400" aria-hidden="true" />}
                <Icon className={`h-[18px] w-[18px] ${active ? 'text-white' : 'text-white/60 group-hover:text-white'}`} />
                <span>{label}</span>
                {countPill(id)}
              </button>
            );
          })}

          {onOpenSimulator && (
            <>
              <p className="px-3 pb-2 pt-6 text-[11px] font-semibold uppercase tracking-wider text-white/50">Demo tools</p>
              <button
                onClick={onOpenSimulator}
                className="group flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium text-white/70 transition-colors hover:bg-white/10 hover:text-white"
              >
                <Mic className="h-[18px] w-[18px] text-white/60 group-hover:text-white" />
                <span>Simulate voice note</span>
              </button>
            </>
          )}
        </nav>

        {availability && <DutyToggle onDuty={availability.onDuty} onChange={availability.onChange} />}

        <div className="mx-3 mb-3 rounded-lg bg-white/10 px-3 py-2.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-white/70">Case feed</span>
            <LiveIndicator live={casesLive} liveLabel="Connected" offLabel="Polling" onDark />
          </div>
        </div>

        <div className="border-t border-white/10 px-4 py-4">{userBlock}</div>
        <p className="px-4 pb-3 text-[10px] text-white/40">Built by SUDO Consultants · AWS Premier Partner</p>
      </aside>

      {/* Mobile top bar */}
      <header className="sticky top-0 z-40 bg-brand-600 lg:hidden">
        <div className="flex h-14 items-center justify-between gap-3 px-4">
          <Brand />
          <div className="flex items-center gap-1">
            {availability && <DutyToggle compact onDuty={availability.onDuty} onChange={availability.onChange} />}
            {onOpenSimulator && (
              <button
                onClick={onOpenSimulator}
                title="Simulate voice note"
                aria-label="Simulate voice note"
                className="rounded-md p-2 text-white/80 hover:bg-white/10 hover:text-white"
              >
                <Mic className="h-5 w-5" />
              </button>
            )}
            {authEnabled && user && (
              <button
                onClick={onLogout}
                title="Sign out"
                aria-label="Sign out"
                className="rounded-md p-2 text-white/80 hover:bg-white/10 hover:text-white"
              >
                <LogOut className="h-5 w-5" />
              </button>
            )}
          </div>
        </div>
        <nav className="flex gap-1 overflow-x-auto border-t border-white/10 px-2 py-2">
          {tabs.map(({ id, label, Icon }) => {
            const active = activeTab === id;
            return (
              <button
                key={id}
                onClick={() => setActiveTab(id)}
                className={`flex shrink-0 items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  active ? 'bg-white/15 text-white' : 'text-white/70 hover:text-white'
                }`}
              >
                <Icon className="h-4 w-4" />
                <span>{label}</span>
                {countPill(id)}
              </button>
            );
          })}
        </nav>
      </header>
    </>
  );
}
