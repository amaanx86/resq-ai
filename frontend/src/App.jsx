import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import Navbar from './components/Navbar';
import EmergencyConsole from './components/EmergencyConsole';
import DoctorRegistration from './components/DoctorRegistration';
import AdminUsers from './components/AdminUsers';
import AnalyticsDashboard from './components/AnalyticsDashboard';
import AuditLogs from './components/AuditLogs';
import VideoCallModal from './components/VideoCallModal';
import SimulateVoiceModal from './components/SimulateVoiceModal';
import IncomingOfferModal from './components/IncomingOfferModal';
import AdminAlerts from './components/AdminAlerts';
import { isOfferedTo, playOfferAlert } from './components/offers';
import LoginScreen from './auth/LoginScreen';
import { api } from './services/api';
import { useAuth } from './hooks/useAuth';
import { useReconnectingWebSocket } from './hooks/useReconnectingWebSocket';

const OPEN_STATUSES = ['ACTIVE', 'DOCTOR_HANDOVER'];
const CASE_POLL_FALLBACK_MS = 5000;  // /ws/cases down
const CASE_POLL_LIVE_MS = 30000;     // /ws/cases up: slow safety net only

const ADMIN_TABS = ['console', 'doctors', 'admins', 'analytics', 'logs'];
const DOCTOR_TABS = ['console', 'logs'];

/** `?case=<id>` deep link (WhatsApp messages to doctors link here); read once, then removed. */
function takeCaseFromUrl() {
  const url = new URL(window.location.href);
  const caseId = url.searchParams.get('case');
  if (caseId) {
    url.searchParams.delete('case');
    window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
  }
  return caseId;
}

export default function App() {
  const auth = useAuth();

  if (auth.enabled) {
    if (!auth.ready || !auth.user) {
      return (
        <LoginScreen
          ready={auth.ready}
          reason={auth.reason}
          configProblems={auth.configProblems}
          onLogin={() => auth.login()}
        />
      );
    }
    if (!auth.user.isAdmin && !auth.user.isDoctor) {
      return (
        <LoginScreen
          ready
          reason={`${auth.user.email || auth.user.name} is signed in but is not in the Administrators or Doctors group. Ask an administrator for access.`}
          onLogin={() => auth.logout()}
          actionLabel="Sign out and use another account"
        />
      );
    }
  }

  return <Portal user={auth.user} authEnabled={auth.enabled} onLogout={auth.logout} />;
}

function Portal({ user, authEnabled, onLogout }) {
  const isAdmin = Boolean(user?.isAdmin);
  const allowedTabs = isAdmin ? ADMIN_TABS : DOCTOR_TABS;

  const [activeTab, setActiveTab] = useState('console');
  const [activeCases, setActiveCases] = useState([]);
  const [lastCaseEvent, setLastCaseEvent] = useState(null);
  const [selectedVideoCase, setSelectedVideoCase] = useState(null);
  const [showSimulator, setShowSimulator] = useState(false);
  // Case to open in the console: from a `?case=` link or an accepted offer.
  const [focusCaseId, setFocusCaseId] = useState(takeCaseFromUrl);
  // Offers this doctor closed without answering (they stay in the queue with their countdown).
  const [dismissedOffers, setDismissedOffers] = useState(() => new Set());
  const doctorId = user?.doctorId || null;
  // A signed-in doctor's own profile, for their on/off duty switch in the navigation.
  const isDoctorView = authEnabled && !isAdmin && Boolean(doctorId);
  const [myProfile, setMyProfile] = useState(null);

  useEffect(() => {
    if (!allowedTabs.includes(activeTab)) setActiveTab('console');
  }, [allowedTabs, activeTab]);

  const refreshCases = useCallback(async () => {
    try {
      const active = await api.getActiveCases();
      setActiveCases(active);
    } catch (err) {
      // In offline / dev mode, handle gracefully
    }
  }, []);

  const upsertCase = useCallback((c) => {
    if (!c?.case_id) return;
    // The backend filters /ws/cases per user; this is a second guard for doctors: their own cases
    // and cases currently offered to them (OFFER-1). A case taken by someone else leaves the queue.
    if (authEnabled && !isAdmin && c.assigned_doctor_id !== doctorId && !isOfferedTo(c, doctorId)) {
      setActiveCases((prev) => prev.filter((x) => x.case_id !== c.case_id));
      return;
    }
    setLastCaseEvent(c);
    setActiveCases((prev) => {
      const rest = prev.filter((x) => x.case_id !== c.case_id);
      if (!OPEN_STATUSES.includes(c.status)) return rest;
      const existing = prev.find((x) => x.case_id === c.case_id);
      const merged = existing ? { ...existing, ...c } : c;
      return existing
        ? prev.map((x) => (x.case_id === c.case_id ? merged : x))
        : [merged, ...rest];
    });
  }, [authEnabled, isAdmin, doctorId]);

  const loadMyProfile = useCallback(async () => {
    if (!isDoctorView) return;
    try {
      setMyProfile(await api.getDoctor(doctorId));
    } catch (err) {
      // Keep the last known state; no switch until the profile has loaded once.
    }
  }, [isDoctorView, doctorId]);

  // Again on focus: an administrator may have changed it from the roster.
  useEffect(() => {
    loadMyProfile();
    window.addEventListener('focus', loadMyProfile);
    return () => window.removeEventListener('focus', loadMyProfile);
  }, [loadMyProfile]);

  const setOnDuty = useCallback(async (isActive) => {
    setMyProfile(await api.toggleDoctorStatus(doctorId, isActive));
  }, [doctorId]);

  // Portal-wide case feed (BRIDGE-1): new and updated cases without polling.
  const handleCaseEvent = useCallback((event) => {
    if (event.type === 'case_upserted' && event.case) upsertCase(event.case);
  }, [upsertCase]);
  const casesLive = useReconnectingWebSocket('/ws/cases', handleCaseEvent);

  useEffect(() => {
    refreshCases();
    const interval = setInterval(refreshCases, casesLive ? CASE_POLL_LIVE_MS : CASE_POLL_FALLBACK_MS);
    return () => clearInterval(interval);
  }, [refreshCases, casesLive]);

  // OFFER-2: cases offered to this doctor -> "Incoming case" popup (oldest first) and an alert sound
  // for each new offer round.
  const myOffers = useMemo(
    () => activeCases.filter((c) => isOfferedTo(c, doctorId)).sort((a, b) => (a.created_at || '').localeCompare(b.created_at || '')),
    [activeCases, doctorId]
  );
  const popupOffer = myOffers.find((c) => !dismissedOffers.has(c.offer_id)) || null;
  const alerted = useRef(new Set());
  useEffect(() => {
    const fresh = myOffers.filter((c) => !alerted.current.has(c.offer_id));
    fresh.forEach((c) => alerted.current.add(c.offer_id));
    if (fresh.length) playOfferAlert();
  }, [myOffers]);

  const openCase = useCallback((caseData) => {
    if (caseData?.case_id) {
      upsertCase(caseData);
      setFocusCaseId(caseData.case_id);
    }
    setActiveTab('console');
  }, [upsertCase]);

  const handleSimulatedSuccess = (caseData) => {
    if (caseData) upsertCase(caseData);
    refreshCases();
    setActiveTab('console');
  };

  return (
    <div className="min-h-screen bg-canvas text-ink lg:pl-64">
      {/* Navigation: sidebar on desktop, top bar on mobile */}
      <Navbar
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        activeCount={activeCases.length}
        allowedTabs={allowedTabs}
        user={user}
        authEnabled={authEnabled}
        onLogout={onLogout}
        casesLive={casesLive}
        onOpenSimulator={isAdmin ? () => setShowSimulator(true) : null}
        availability={myProfile ? { onDuty: Boolean(myProfile.is_active), onChange: setOnDuty } : null}
      />

      <div className="flex min-h-screen flex-col">
        {/* No-doctor alerts (ADMIN-1): administrators, on every tab */}
        {isAdmin && <AdminAlerts cases={activeCases} onOpenCase={openCase} onCaseUpdated={upsertCase} />}

        {/* Main Tab Content */}
        <main className="flex-1">
          {activeTab === 'console' && (
            <EmergencyConsole
              cases={activeCases}
              casesLive={casesLive}
              onRefreshCases={refreshCases}
              onCaseResolved={(c) => upsertCase({ ...c, status: c.status || 'RESOLVED' })}
              onCaseUpdated={upsertCase}
              onOpenVideoCall={(caseData) => setSelectedVideoCase(caseData)}
              focusCaseId={focusCaseId}
              onFocusHandled={() => setFocusCaseId(null)}
              user={user}
              authEnabled={authEnabled}
            />
          )}
          {activeTab === 'doctors' && isAdmin && <DoctorRegistration />}
          {activeTab === 'admins' && isAdmin && <AdminUsers user={user} />}
          {activeTab === 'analytics' && isAdmin && <AnalyticsDashboard />}
          {activeTab === 'logs' && <AuditLogs caseEvent={lastCaseEvent} isAdmin={isAdmin} />}
        </main>

        {/* Footer */}
        <footer className="border-t border-slate-200 px-4 py-4 text-xs text-slate-400 sm:px-6 lg:px-8">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-2">
            <span>ResQ-AI Clinical Emergency Triage Platform</span>
            <span>Powered by Strands Agents &amp; Amazon Bedrock AgentCore</span>
          </div>
        </footer>
      </div>

      {/* Video Call Modal */}
      {selectedVideoCase && (
        <VideoCallModal
          caseData={selectedVideoCase}
          onClose={() => setSelectedVideoCase(null)}
        />
      )}

      {/* Incoming case offer (OFFER-2) */}
      {popupOffer && !selectedVideoCase && (
        <IncomingOfferModal
          key={popupOffer.offer_id}
          caseData={popupOffer}
          doctorId={doctorId}
          onAnswered={upsertCase}
          onAccepted={openCase}
          onDismiss={() => setDismissedOffers((prev) => new Set(prev).add(popupOffer.offer_id))}
        />
      )}

      {/* Simulated Voice Note Modal */}
      {showSimulator && (
        <SimulateVoiceModal
          onClose={() => setShowSimulator(false)}
          onSimulated={handleSimulatedSuccess}
        />
      )}
    </div>
  );
}
