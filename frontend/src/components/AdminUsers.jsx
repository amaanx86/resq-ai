import React, { useState, useEffect } from 'react';
import { UserPlus, ShieldCheck, Phone, Mail, Check, Loader2, Send, AlertCircle, BellRing, BellOff, Pencil, Trash2, X } from 'lucide-react';
import { api } from '../services/api';
import { Avatar, Badge, PageHeader, EmptyState, Switch } from './ui';

// Administrators added here (ADMIN-2, backend/app/routers/admins.py): a Cognito portal user in the
// Administrators group (invitation email) and a phone number that gets the no-doctor alert calls.
const EMPTY_FORM = { name: '', email: '', phone_number: '', receive_alert_calls: true };

const formatDate = (iso) =>
  iso ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '';

const errorText = (err, fallback) => {
  const detail = err?.response?.data?.detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join('; ') || fallback;
  return detail || fallback;
};

function PortalStatus({ admin }) {
  if (admin.portal_status === 'INVITED') {
    return <Badge tone="amber" icon={Mail}>Invited {formatDate(admin.portal_invited_at)}</Badge>;
  }
  if (admin.portal_status === 'ACTIVE') return <Badge tone="brand" icon={Check}>Portal access</Badge>;
  return null;
}

function AdminRow({ admin, isMe, onChanged, onRemoved }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ name: admin.name, phone_number: admin.phone_number });
  const [busy, setBusy] = useState(null); // 'save' | 'calls' | 'invite' | 'remove'
  const [msg, setMsg] = useState(null);   // { tone: 'ok' | 'error', text }
  const [confirmRemove, setConfirmRemove] = useState(false);

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

  const save = async (e) => {
    e.preventDefault();
    const updated = await run('save', () => api.updateAdmin(admin.admin_id, draft), 'Saved.');
    if (updated) { onChanged(updated); setEditing(false); }
  };

  const toggleCalls = async () => {
    const updated = await run('calls', () => api.updateAdmin(admin.admin_id, { receive_alert_calls: !admin.receive_alert_calls }));
    if (updated) onChanged(updated);
  };

  const resend = async () => {
    const updated = await run('invite', () => api.inviteAdminToPortal(admin.admin_id), (u) => (
      u.portal_status === 'ACTIVE' ? `${u.email} has already set a password.`
        : u.portal_status === 'SKIPPED' ? 'Portal invitations are off here (no Cognito user pool).'
          : `Invitation sent again to ${u.email}.`));
    if (updated) onChanged(updated);
  };

  const remove = async () => {
    const result = await run('remove', () => api.removeAdmin(admin.admin_id));
    if (result) onRemoved(admin.admin_id);
    setConfirmRemove(false);
  };

  return (
    <li className="flex items-start gap-4 px-5 py-4">
      <Avatar name={admin.name} className="h-10 w-10 text-sm" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-brand-900">{admin.name}</h3>
          {isMe && <Badge tone="brand">You</Badge>}
          <PortalStatus admin={admin} />
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
          <span className="flex min-w-0 items-center gap-1">
            <Mail className="h-3 w-3 shrink-0" /> <span className="truncate">{admin.email}</span>
          </span>
          <span className="flex items-center gap-1 font-mono">
            <Phone className="h-3 w-3" /> {admin.phone_number}
          </span>
          {admin.added_by && <span>added by {admin.added_by}</span>}
        </div>

        {editing ? (
          <form onSubmit={save} className="mt-2 flex flex-wrap items-center gap-2">
            <input
              aria-label="Name"
              required
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              className="input h-8 w-48 max-w-full py-1 text-sm"
            />
            <input
              aria-label="Phone number"
              required
              value={draft.phone_number}
              onChange={(e) => setDraft({ ...draft, phone_number: e.target.value })}
              className="input h-8 w-44 max-w-full py-1 font-mono text-sm"
            />
            <button type="submit" disabled={busy === 'save'} className="btn btn-primary h-8 px-3 py-1 text-xs">
              {busy === 'save' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
              Save
            </button>
            <button type="button" onClick={() => { setEditing(false); setDraft({ name: admin.name, phone_number: admin.phone_number }); }}
              className="text-xs text-slate-500 hover:text-slate-700">
              Cancel
            </button>
          </form>
        ) : (
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
            <button type="button" onClick={() => { setEditing(true); setMsg(null); }}
              className="link inline-flex items-center gap-1 text-xs">
              <Pencil className="h-3 w-3" /> Edit
            </button>
            {admin.portal_status === 'INVITED' && (
              <button type="button" onClick={resend} disabled={busy === 'invite'}
                className="link inline-flex items-center gap-1 text-xs disabled:opacity-60">
                {busy === 'invite' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Send className="h-3 w-3" />}
                Resend invitation
              </button>
            )}
            {!isMe && !confirmRemove && (
              <button type="button" onClick={() => setConfirmRemove(true)}
                className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 hover:text-red-700">
                <Trash2 className="h-3 w-3" /> Remove
              </button>
            )}
            {confirmRemove && (
              <span className="inline-flex items-center gap-2 text-xs text-red-800">
                Remove {admin.name}'s admin access?
                <button type="button" onClick={remove} disabled={busy === 'remove'}
                  className="btn btn-danger h-6 px-2 py-0 text-xs">
                  {busy === 'remove' ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
                  Remove
                </button>
                <button type="button" onClick={() => setConfirmRemove(false)} className="text-slate-500 hover:text-slate-700">
                  <X className="h-3 w-3" />
                </button>
              </span>
            )}
          </div>
        )}
        {msg && (
          <p className={`mt-1 text-xs ${msg.tone === 'error' ? 'text-red-700' : 'text-brand-700'}`}>{msg.text}</p>
        )}
      </div>

      {/* Alert-call switch */}
      <div className="flex shrink-0 flex-col items-end gap-1">
        <Switch checked={admin.receive_alert_calls} onChange={toggleCalls} disabled={busy === 'calls'}
          label={`${admin.name} gets no-doctor alert calls`} />
        <span className={`flex items-center gap-1 text-xs font-medium ${admin.receive_alert_calls ? 'text-brand-700' : 'text-slate-500'}`}>
          {admin.receive_alert_calls ? <BellRing className="h-3 w-3" /> : <BellOff className="h-3 w-3" />}
          {admin.receive_alert_calls ? 'Alert calls' : 'No calls'}
        </span>
      </div>
    </li>
  );
}

export default function AdminUsers({ user }) {
  const [admins, setAdmins] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [formData, setFormData] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [successMsg, setSuccessMsg] = useState('');
  const [errorMsg, setErrorMsg] = useState('');
  const myEmail = (user?.email || '').toLowerCase();

  useEffect(() => {
    api.getAdmins()
      .then((data) => { setAdmins(data); setLoadError(''); })
      .catch((err) => setLoadError(errorText(err, 'The administrators could not be loaded.')))
      .finally(() => setLoaded(true));
  }, []);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setErrorMsg('');
    setSuccessMsg('');
    try {
      const created = await api.addAdmin(formData);
      setAdmins((prev) => [...prev, created]);
      const access = {
        INVITED: `An invitation to the portal was emailed to ${created.email}.`,
        ACTIVE: `${created.email} already had a portal sign-in; it now has administrator access.`,
        SKIPPED: 'Portal invitations are off here (no Cognito user pool).',
      }[created.portal_status] || '';
      const calls = created.receive_alert_calls ? ` ${created.phone_number} gets the no-doctor alert calls.` : '';
      setSuccessMsg(`${created.name} added. ${access}${calls}`);
      setFormData(EMPTY_FORM);
      setTimeout(() => setSuccessMsg(''), 10000);
    } catch (err) {
      setErrorMsg(errorText(err, 'The administrator could not be added. Please try again.'));
    } finally {
      setSaving(false);
    }
  };

  const onChanged = (updated) => setAdmins((prev) => prev.map((a) => (a.admin_id === updated.admin_id ? updated : a)));
  const onRemoved = (adminId) => setAdmins((prev) => prev.filter((a) => a.admin_id !== adminId));
  const callCount = admins.filter((a) => a.receive_alert_calls).length;

  return (
    <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
      <PageHeader
        title="Administrators"
        description="Administrators see every case and get the alerts for cases without a doctor. Those with alert calls on are phoned through Amazon Connect when an ESI 1–2 case has no doctor."
        actions={
          <div className="flex items-center divide-x divide-slate-200 rounded-lg border border-slate-200 bg-white text-sm shadow-card">
            <div className="px-4 py-2">
              <span className="block text-xs text-slate-500">Alert calls</span>
              <span className="font-display font-semibold tabular-nums text-brand-900">{callCount}</span>
            </div>
            <div className="px-4 py-2">
              <span className="block text-xs text-slate-500">Administrators</span>
              <span className="font-display font-semibold tabular-nums text-brand-900">{admins.length}</span>
            </div>
          </div>
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <div className="lg:col-span-5">
          <div className="card">
            <div className="card-header">
              <h2 className="card-title flex items-center gap-2">
                <UserPlus className="h-4 w-4 text-brand-500" />
                Add an administrator
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
                <label htmlFor="adm-name" className="label">Full name</label>
                <input id="adm-name" type="text" required placeholder="e.g. Ayesha Malik"
                  value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })} className="input" />
              </div>
              <div>
                <label htmlFor="adm-email" className="label">Email</label>
                <input id="adm-email" type="email" required autoComplete="off" placeholder="ayesha.malik@hospital.pk"
                  value={formData.email} onChange={(e) => setFormData({ ...formData, email: e.target.value })} className="input" />
                <span className="hint">We email the portal invitation here; it is also their sign-in</span>
              </div>
              <div>
                <label htmlFor="adm-phone" className="label">Mobile number</label>
                <input id="adm-phone" type="tel" required placeholder="+923001234567"
                  value={formData.phone_number} onChange={(e) => setFormData({ ...formData, phone_number: e.target.value })} className="input font-mono" />
                <span className="hint">Called when an ESI 1–2 case has no doctor</span>
              </div>
              <label htmlFor="adm-calls" className="flex cursor-pointer items-start gap-3 rounded-lg border border-slate-200 p-3 hover:bg-canvas">
                <input type="checkbox" id="adm-calls" checked={formData.receive_alert_calls}
                  onChange={(e) => setFormData({ ...formData, receive_alert_calls: e.target.checked })}
                  className="mt-0.5 h-4 w-4 rounded border-slate-300 accent-brand-600" />
                <span>
                  <span className="block text-sm font-medium text-ink">Receive no-doctor alert calls</span>
                  <span className="block text-xs text-slate-500">Every 5 minutes until someone acknowledges the alert in the portal</span>
                </span>
              </label>
              <button type="submit" disabled={saving} className="btn btn-primary w-full">
                {saving && <Loader2 className="h-4 w-4 animate-spin" />}
                {saving ? 'Adding...' : 'Add & send invitation'}
              </button>
            </form>
          </div>
        </div>

        <div className="lg:col-span-7">
          <div className="card overflow-hidden">
            <div className="card-header">
              <h2 className="card-title flex items-center gap-2">
                <ShieldCheck className="h-4 w-4 text-brand-500" />
                Administrators
              </h2>
              <span className="text-xs tabular-nums text-slate-500">{callCount} of {admins.length} get alert calls</span>
            </div>
            {loadError ? (
              <p role="alert" className="px-5 py-4 text-sm text-red-700">{loadError}</p>
            ) : loaded && admins.length === 0 ? (
              <EmptyState icon={ShieldCheck} title="No administrators added here yet"
                description="Administrators created by hand in Cognito can sign in, but have no phone number for alert calls until they are added here." />
            ) : (
              <ul className="max-h-[640px] divide-y divide-slate-100 overflow-y-auto">
                {admins.map((a) => (
                  <AdminRow key={a.admin_id} admin={a} isMe={Boolean(myEmail) && a.email.toLowerCase() === myEmail}
                    onChanged={onChanged} onRemoved={onRemoved} />
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
