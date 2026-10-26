import React, { useState } from 'react';
import { LogIn, ShieldAlert, Loader2, Languages, Truck, Video } from 'lucide-react';
import { BrandMark } from '../components/ui';

const HIGHLIGHTS = [
  { Icon: Languages, title: 'Multilingual triage', text: 'Urdu, Hindi and English WhatsApp voice notes, classified to ESI 1–5.' },
  { Icon: Truck, title: 'Doctor-led ambulance', text: 'Emergency-contact calls and shared patient location for ESI 1–2 cases.' },
  { Icon: Video, title: 'Specialist handover', text: 'Live chat bridge and one-click video consultations.' },
];

/** Shown when portal auth is on and nobody is signed in (or the session expired). */
export default function LoginScreen({ ready, reason, configProblems = [], onLogin, actionLabel = 'Sign in with Amazon Cognito' }) {
  const [redirecting, setRedirecting] = useState(false);
  const [error, setError] = useState(null);

  const handleLogin = async () => {
    setError(null);
    setRedirecting(true);
    try {
      await onLogin();
    } catch (e) {
      setError(e.message || String(e));
      setRedirecting(false);
    }
  };

  return (
    <div className="flex min-h-screen bg-canvas">
      {/* Brand panel */}
      <div className="relative hidden w-[44%] max-w-xl flex-col justify-between overflow-hidden bg-brand-gradient p-12 lg:flex">
        <div className="relative flex items-center gap-2.5">
          <BrandMark className="h-9 w-9 bg-none bg-white/15 ring-1 ring-white/25" iconClassName="h-5 w-5" />
          <span className="font-display text-lg font-semibold tracking-tight text-white">ResQ-AI</span>
        </div>

        <div className="relative">
          <h2 className="font-display text-3xl font-semibold leading-tight tracking-tight text-white">
            Emergency triage and specialist handover, in one console.
          </h2>
          <ul className="mt-10 space-y-6">
            {HIGHLIGHTS.map(({ Icon, title, text }) => (
              <li key={title} className="flex gap-4">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-white/15 ring-1 ring-white/20">
                  <Icon className="h-[18px] w-[18px] text-white" />
                </div>
                <div>
                  <p className="text-sm font-medium text-white">{title}</p>
                  <p className="mt-0.5 text-sm text-white/75">{text}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>

        <p className="relative text-xs text-white/70">Built by SUDO Consultants · Strands Agents &amp; Amazon Bedrock AgentCore</p>
      </div>

      {/* Sign-in panel */}
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="w-full max-w-sm">
          <div className="mb-8 flex items-center gap-2.5 lg:hidden">
            <BrandMark className="h-9 w-9" iconClassName="h-5 w-5" />
            <span className="font-display text-lg font-semibold tracking-tight text-brand-900">ResQ-AI</span>
          </div>

          <h1 className="font-display text-2xl font-semibold tracking-tight text-brand-900">Sign in to the clinical portal</h1>
          <p className="mt-2 text-sm text-slate-500">
            Use your hospital account. Access is limited to administrators and on-duty doctors.
          </p>

          {reason && (
            <div className="mt-6 flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{reason}</span>
            </div>
          )}
          {configProblems.length > 0 && (
            <div className="mt-6 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              Sign-in is not configured. Missing build settings: <span className="font-mono text-xs">{configProblems.join(', ')}</span>
            </div>
          )}
          {error && (
            <div className="mt-6 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {error}
            </div>
          )}

          <button
            onClick={handleLogin}
            disabled={!ready || redirecting || configProblems.length > 0}
            className="btn btn-primary mt-8 w-full py-2.5"
          >
            {!ready || redirecting ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />}
            <span>{!ready ? 'Checking session...' : redirecting ? 'Redirecting...' : actionLabel}</span>
          </button>

          <p className="mt-6 text-center text-xs text-slate-400">
            In a life-threatening emergency, call your local emergency number.
          </p>
        </div>
      </div>
    </div>
  );
}
