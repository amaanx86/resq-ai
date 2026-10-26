import React, { useState, useEffect } from 'react';
import {
  UserPlus, Users, Phone, ShieldCheck, Check, Loader2, Briefcase, Mail, Send, AlertCircle, Pencil, Trash2,
  X, MessageCircleQuestion, BadgeCheck, Ban,
} from 'lucide-react';
import { api } from '../services/api';
import { Avatar, Badge, PageHeader, EmptyState, Switch } from './ui';

const EMPTY_FORM = {
  name: '',
  email: '',
  phone_number: '',
  license_no: '',
  specialty: 'Cardiology',
  bio: '',
  is_active: true
};

const formatDate = (iso) =>
  iso ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '';

const errorText = (err, fallback) => {
  const detail = err?.response?.data?.detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join('; ') || fallback;
  return detail || fallback;
};

const SPECIALTIES = [
  "Cardiology",
  "Neurology",
  "Trauma",
  "Pulmonology",
  "Emergency Medicine",
  "Pediatrics"
];

// Portal sign-in state (backend/app/services/cognito_service.py).
function PortalStatus({ doctor }) {
  if (!doctor.email) return <Badge tone="gray">No portal sign-in</Badge>;
  if (doctor.portal_status === 'INVITED') {
    return <Badge tone="amber" icon={Mail}>Invited {formatDate(doctor.portal_invited_at)}</Badge>;
  }
  if (doctor.portal_status === 'ACTIVE') return <Badge tone="brand" icon={Check}>Portal access</Badge>;
  return null;
}

// WhatsApp number confirmation (DOC-8, backend/app/doctor_verification.py). None: registered before
// confirmation existed (trusted).
function NumberStatus({ doctor }) {
  if (doctor.phone_status === 'PENDING') {
    return (
      <Badge tone="amber" icon={MessageCircleQuestion}
        title={doctor.phone_confirmation_status === 'FAILED' ? 'The WhatsApp question could not be sent' : 'Waiting for the doctor to tap "Yes, it\'s me"'}>
        Number not confirmed{doctor.phone_confirmation_status === 'FAILED' ? ' · not sent' : ''}
      </Badge>
    );
  }
  if (doctor.phone_status === 'DECLINED') return <Badge tone="red" icon={Ban}>Number owner said "not me"</Badge>;
  if (doctor.phone_status === 'CONFIRMED') return <Badge tone="accent" icon={BadgeCheck}>Number confirmed</Badge>;
  return null;
}

function DoctorRow({ doctor, onChanged, onRemoved }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(null);   // 'save' | 'duty' | 'invite' | 'confirm' | 'remove'
  const [msg, setMsg] = useState(null);     // { tone: 'ok' | 'error', text }
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [emailOpen, setEmailOpen] = useState(false);
  const [inviteEmail, setInviteEmail] = useState('');

  const run = async (kind, action, okText) => {
    setBusy(kind);
    setMsg(null);
    try {
      const result = await action();
      if (okText) setMsg({ tone: 'ok', text: typeof okText === 'function' ? okText(result) : okText });
      return result;
    } catch (err) {
      setMsg({ tone: 'error', text: errorText(err, 'That did not work. Please try again.') });
      return null;
    } finally {
      setBusy(null);
    }
  };

  const startEdit = () => {
    setDraft({ name: doctor.name, phone_number: doctor.phone_number, license_no: doctor.license_no,
      specialty: doctor.specialty, bio: doctor.bio || '' });
    setEditing(true);
    setMsg(null);
  };

  const save = async (e) => {
    e.preventDefault();
    const updated = await run('save', () => api.updateDoctor(doctor.doctor_id, draft), (u) => (
      u.phone_status === 'PENDING' && u.phone_number !== doctor.phone_number
        ? `Saved. ${u.phone_number} gets a WhatsApp question to confirm it is the doctor's number.`
        : 'Saved.'));
    if (updated) { onChanged(updated); setEditing(false); }
  };

  const toggleDuty = async () => {
    const updated = await run('duty', () => api.toggleDoctorStatus(doctor.doctor_id, !doctor.is_active));
    if (updated) onChanged(updated);
  };

  const invite = async (email) => {
    const updated = await run('invite', () => api.inviteDoctorToPortal(doctor.doctor_id, email), (u) => ({
      INVITED: `Invitation ${email ? 'sent' : 'sent again'} to ${u.email}.`,
      ACTIVE: `${u.email} has already set a password; it now opens this doctor's cases.`,
      SKIPPED: 'Email saved. Portal invitations are off here (no Cognito user pool).',
    }[u.portal_status] || 'Saved.'));
    if (updated) { onChanged(updated); setEmailOpen(false); setInviteEmail(''); }
  };

  const resendConfirmation = async () => {
    const updated = await run('confirm', () => api.resendNumberConfirmation(doctor.doctor_id),
      (u) => `Asked ${u.phone_number} again on WhatsApp.`);
    if (updated) onChanged(updated);
  };

  const remove = async () => {
    const result = await run('remove', () => api.unregisterDoctor(doctor.doctor_id));
    setConfirmRemove(false);
    if (result) onRemoved(doctor.doctor_id);
  };

  const field = (name, props = {}) => (
    <input
      aria-label={name.replace('_', ' ')}
      required={name !== 'bio'}
      value={draft[name]}
      onChange={(e) => setDraft({ ...draft, [name]: e.target.value })}
      className={`input h-8 py-1 text-sm ${props.className || ''}`}
      placeholder={props.placeholder}
    />
  );

  return (
    <li className="flex items-start gap-4 px-5 py-4">
      <Avatar
        name={doctor.name}
        className="h-10 w-10 text-sm"
        tone={doctor.is_active ? 'bg-brand-50 text-brand-700 ring-brand-600/10' : 'bg-slate-100 text-slate-500 ring-slate-500/10'}
      />

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-brand-900">{doctor.name}</h3>
          <Badge tone="gray">{doctor.specialty}</Badge>
          <PortalStatus doctor={doctor} />
          <NumberStatus doctor={doctor} />
        </div>

        <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
          {doctor.email && (
            <span className="flex min-w-0 items-center gap-1">
              <Mail className="h-3 w-3 shrink-0" /> <span className="truncate">{doctor.email}</span>
            </span>
          )}
          <span className="flex items-center gap-1 font-mono">
            <Phone className="h-3 w-3" /> {doctor.phone_number}
          </span>
          <span className="flex items-center gap-1">
            <ShieldCheck className="h-3 w-3" /> {doctor.license_no}
          </span>
          <span className="flex items-center gap-1">
            <Briefcase className="h-3 w-3" /> <span className="tabular-nums">{doctor.cases_handled ?? 0}</span> cases
          </span>
        </div>

        {!editing && doctor.bio && <p className="mt-1.5 line-clamp-2 text-sm text-slate-600">{doctor.bio}</p>}

        {editing ? (
          <form onSubmit={save} className="mt-3 grid grid-cols-1 gap-2 rounded-lg border border-slate-200 bg-canvas p-3 sm:grid-cols-2">
            {field('name', { placeholder: 'Full name' })}
            {field('phone_number', { placeholder: '+923001234567', className: 'font-mono' })}
            {field('license_no', { placeholder: 'Licence no.' })}
            <select aria-label="specialty" value={draft.specialty}
              onChange={(e) => setDraft({ ...draft, specialty: e.target.value })} className="input h-8 py-1 text-sm">
              {[...new Set([...SPECIALTIES, draft.specialty])].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <textarea aria-label="bio" rows="2" value={draft.bio} placeholder="Bio (optional)"
              onChange={(e) => setDraft({ ...draft, bio: e.target.value })} className="input text-sm sm:col-span-2" />
            <p className="text-xs text-slate-500 sm:col-span-2">
              The sign-in email stays {doctor.email || 'unset'}. A new number is asked on WhatsApp to confirm before it gets cases.
            </p>
            <div className="flex items-center gap-2 sm:col-span-2">
              <button type="submit" disabled={busy === 'save'} className="btn btn-primary btn-sm">
                {busy === 'save' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
                Save changes
              </button>
              <button type="button" onClick={() => setEditing(false)} className="btn btn-ghost btn-sm">Cancel</button>
            </div>
          </form>
        ) : (
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
            <button type="button" onClick={startEdit} className="link inline-flex items-center gap-1">
              <Pencil className="h-3 w-3" /> Edit
            </button>
            {(doctor.phone_status === 'PENDING' || doctor.phone_status === 'DECLINED') && (
              <button type="button" onClick={resendConfirmation} disabled={busy === 'confirm'}
                className="link inline-flex items-center gap-1 disabled:opacity-60">
                {busy === 'confirm' ? <Loader2 className="h-3 w-3 animate-spin" /> : <MessageCircleQuestion className="h-3 w-3" />}
                Ask to confirm the number
              </button>
            )}
            {!doctor.email && !emailOpen && (
              <button type="button" onClick={() => { setEmailOpen(true); setMsg(null); }} className="link inline-flex items-center gap-1">
                <Send className="h-3 w-3" /> Invite to portal
              </button>
            )}
            {doctor.email && doctor.portal_status === 'INVITED' && (
              <button type="button" onClick={() => invite()} disabled={busy === 'invite'}
                className="link inline-flex items-center gap-1 disabled:opacity-60">
                {busy === 'invite' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Send className="h-3 w-3" />}
                Resend invitation
              </button>
            )}
            {!confirmRemove ? (
              <button type="button" onClick={() => { setConfirmRemove(true); setMsg(null); }}
                className="inline-flex items-center gap-1 font-medium text-slate-500 hover:text-red-700">
                <Trash2 className="h-3 w-3" /> Unregister
              </button>
            ) : (
              <span className="inline-flex flex-wrap items-center gap-2 rounded-md bg-red-50 px-2 py-1 text-red-800">
                Unregister {doctor.name}? They get no more cases and lose portal access.
                <button type="button" onClick={remove} disabled={busy === 'remove'} className="btn btn-danger h-6 px-2 py-0 text-xs">
                  {busy === 'remove' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
                  Unregister
                </button>
                <button type="button" aria-label="Cancel" onClick={() => setConfirmRemove(false)} className="text-slate-500 hover:text-slate-700">
                  <X className="h-3.5 w-3.5" />
                </button>
              </span>
            )}
          </div>
        )}

        {!doctor.email && emailOpen && (
          <form className="mt-2 flex flex-wrap items-center gap-2"
            onSubmit={(e) => { e.preventDefault(); if (inviteEmail) invite(inviteEmail); }}>
            <input type="email" required autoFocus aria-label={`Email for ${doctor.name}`} placeholder="doctor@hospital.pk"
              value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)} className="input h-8 w-64 max-w-full py-1 text-sm" />
            <button type="submit" disabled={busy === 'invite'} className="btn btn-primary btn-sm">
              {busy === 'invite' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Send className="h-3 w-3" />}
              Send invitation
            </button>
            <button type="button" onClick={() => setEmailOpen(false)} className="btn btn-ghost btn-sm">Cancel</button>
          </form>
        )}

        {msg && <p className={`mt-1.5 text-xs ${msg.tone === 'error' ? 'text-red-700' : 'text-brand-700'}`}>{msg.text}</p>}
      </div>

      {/* On / off duty */}
      <div className="flex shrink-0 flex-col items-end gap-1">
        <Switch checked={doctor.is_active} onChange={toggleDuty} disabled={busy === 'duty'} label={`${doctor.name} on duty`} />
        <span className={`text-xs font-medium ${doctor.is_active ? 'text-brand-700' : 'text-slate-500'}`}>
          {doctor.is_active ? 'On duty' : 'Off duty'}
        </span>
      </div>
    </li>
  );
}

export default function DoctorRegistration() {
  const [doctors, setDoctors] = useState([]);
  const [loading, setLoading] = useState(false);
  const [successMsg, setSuccessMsg] = useState('');
  const [errorMsg, setErrorMsg] = useState('');
  const [formData, setFormData] = useState(EMPTY_FORM);

  useEffect(() => {
    api.getDoctors().then(setDoctors).catch((err) => console.error("Failed to load doctors", err));
  }, []);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!formData.name || !formData.email || !formData.phone_number || !formData.license_no) return;

    setLoading(true);
    setErrorMsg('');
    setSuccessMsg('');
    try {
      const created = await api.registerDoctor(formData);
      setDoctors((prev) => [created, ...prev]);
      const access = {
        INVITED: `An invitation to the portal was emailed to ${created.email}.`,
        ACTIVE: `${created.email} already had a portal sign-in; it now opens this doctor's cases.`,
        SKIPPED: 'Portal invitations are off here (no Cognito user pool).',
      }[created.portal_status] || '';
      const number = created.phone_status === 'PENDING'
        ? ` ${created.phone_number} gets a WhatsApp question to confirm the number; cases start once the doctor taps "Yes, it's me".`
        : '';
      setSuccessMsg(`${created.name} registered (ID ${created.doctor_id}). ${access}${number}`);
      setFormData(EMPTY_FORM);
      setTimeout(() => setSuccessMsg(''), 12000);
    } catch (err) {
      setErrorMsg(errorText(err, 'The doctor could not be registered. Please try again.'));
    } finally {
      setLoading(false);
    }
  };

  const onChanged = (updated) => setDoctors((prev) => prev.map((d) => (d.doctor_id === updated.doctor_id ? updated : d)));
  const onRemoved = (doctorId) => setDoctors((prev) => prev.filter((d) => d.doctor_id !== doctorId));
  const onDutyCount = doctors.filter((d) => d.is_active).length;
  const unconfirmed = doctors.filter((d) => d.phone_status === 'PENDING' || d.phone_status === 'DECLINED').length;

  return (
    <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <PageHeader
        title="Doctors & On-Duty Roster"
        description="Incoming WhatsApp emergencies are offered to the free, on-duty doctors of this roster whose WhatsApp number is confirmed."
        actions={
          <div className="flex items-center divide-x divide-slate-200 rounded-lg border border-slate-200 bg-white text-sm shadow-card">
            <div className="px-4 py-2">
              <span className="block text-xs text-slate-500">On duty</span>
              <span className="font-display font-semibold tabular-nums text-brand-900">{onDutyCount}</span>
            </div>
            <div className="px-4 py-2">
              <span className="block text-xs text-slate-500">Registered</span>
              <span className="font-display font-semibold tabular-nums text-brand-900">{doctors.length}</span>
            </div>
            {unconfirmed > 0 && (
              <div className="px-4 py-2">
                <span className="block text-xs text-slate-500">Number not confirmed</span>
                <span className="font-display font-semibold tabular-nums text-amber-700">{unconfirmed}</span>
              </div>
            )}
          </div>
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        {/* Registration form */}
        <div className="lg:col-span-5">
          <div className="card">
            <div className="card-header">
              <h2 className="card-title flex items-center gap-2">
                <UserPlus className="h-4 w-4 text-brand-500" />
                Register a specialist
              </h2>
            </div>

            <form onSubmit={handleSubmit} className="space-y-4 px-5 py-5">
              {successMsg && (
                <div className="notice notice-ok">
                  <Check className="mt-0.5 h-4 w-4 shrink-0 text-brand-600" />
                  <span>{successMsg}</span>
                </div>
              )}
              {errorMsg && (
                <div role="alert" className="notice notice-error">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
                  <span>{errorMsg}</span>
                </div>
              )}

              <div>
                <label htmlFor="doc-name" className="label">Full name &amp; credentials</label>
                <input id="doc-name" type="text" required placeholder="e.g. Dr. Sarah Khan, MD, FACC"
                  value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })} className="input" />
              </div>

              <div>
                <label htmlFor="doc-email" className="label">Email</label>
                <input id="doc-email" type="email" required autoComplete="off" placeholder="sarah.khan@hospital.pk"
                  value={formData.email} onChange={(e) => setFormData({ ...formData, email: e.target.value })} className="input" />
                <span className="hint">We email the portal invitation here; it is also their sign-in</span>
              </div>

              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div>
                  <label htmlFor="doc-phone" className="label">WhatsApp number</label>
                  <input id="doc-phone" type="text" required placeholder="+923001234567"
                    value={formData.phone_number} onChange={(e) => setFormData({ ...formData, phone_number: e.target.value })}
                    className="input font-mono" />
                  <span className="hint">The doctor confirms it on WhatsApp</span>
                </div>
                <div>
                  <label htmlFor="doc-license" className="label">Medical license no.</label>
                  <input id="doc-license" type="text" required placeholder="PMC-88214"
                    value={formData.license_no} onChange={(e) => setFormData({ ...formData, license_no: e.target.value })} className="input" />
                </div>
              </div>

              <div>
                <label htmlFor="doc-specialty" className="label">Specialty / department</label>
                <select id="doc-specialty" value={formData.specialty}
                  onChange={(e) => setFormData({ ...formData, specialty: e.target.value })} className="input">
                  {SPECIALTIES.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
                <span className="hint">The triage agent matches emergencies to this specialty</span>
              </div>

              <div>
                <label htmlFor="doc-bio" className="label">
                  Bio &amp; sub-specialty <span className="font-normal text-slate-400">(optional)</span>
                </label>
                <textarea id="doc-bio" rows="3" placeholder="Interventional cardiologist, acute coronary syndrome, angioplasty..."
                  value={formData.bio} onChange={(e) => setFormData({ ...formData, bio: e.target.value })} className="input" />
              </div>

              <label htmlFor="activeCheck" className="flex cursor-pointer items-start gap-3 rounded-lg border border-slate-200 p-3 hover:bg-canvas">
                <input type="checkbox" id="activeCheck" checked={formData.is_active}
                  onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
                  className="mt-0.5 h-4 w-4 rounded border-slate-300 accent-brand-600" />
                <span>
                  <span className="block text-sm font-medium text-ink">On duty immediately</span>
                  <span className="block text-xs text-slate-500">Available for cases once the number is confirmed</span>
                </span>
              </label>

              <button type="submit" disabled={loading} className="btn btn-primary w-full">
                {loading && <Loader2 className="h-4 w-4 animate-spin" />}
                {loading ? "Registering..." : "Register & send invitation"}
              </button>
            </form>
          </div>
        </div>

        {/* Roster */}
        <div className="lg:col-span-7">
          <div className="card overflow-hidden">
            <div className="card-header">
              <h2 className="card-title flex items-center gap-2">
                <Users className="h-4 w-4 text-brand-500" />
                Specialist roster
              </h2>
              <span className="text-xs tabular-nums text-slate-500">{onDutyCount} of {doctors.length} on duty</span>
            </div>

            {doctors.length === 0 ? (
              <EmptyState icon={Users} title="No doctors registered" description="Register a specialist to start matching emergency cases." />
            ) : (
              <ul className="max-h-[720px] divide-y divide-slate-100 overflow-y-auto">
                {doctors.map((doc) => (
                  <DoctorRow key={doc.doctor_id} doctor={doc} onChanged={onChanged} onRemoved={onRemoved} />
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
