import React, { useEffect, useRef, useState } from 'react';
import {
  Mic, MicOff, Video, VideoOff, PhoneOff, Loader2, AlertTriangle,
  ShieldCheck, RefreshCw, FlaskConical
} from 'lucide-react';
import { videoApi, describeJoinError } from '../services/videoApi';
import { ensureSignedIn } from '../auth/authService';
import {
  requestMediaPermissions, openPreviewStream, stopStream, describePermissionProblem
} from '../video/media';

const PHASE_TEXT = {
  joining: 'Checking your video link...',
  media: 'Allow camera and microphone access when your browser asks.',
  connecting: 'Connecting to the secure video room...'
};

function initials(name, fallback) {
  if (!name) return fallback;
  const parts = name.replace(/^Dr\.?\s+/i, '').split(/\s+/).filter(Boolean);
  return (parts[0]?.[0] || '') + (parts[1]?.[0] || '') || fallback;
}

/**
 * Two-party emergency video room (Amazon Chime SDK). Used standalone on /call/... (patient
 * phone or doctor tab) and embedded in the portal's VideoCallModal (role=doctor).
 */
export default function VideoRoom({ meetingId, caseId, role = 'patient', token, embedded = false, onLeave }) {
  const [phase, setPhase] = useState('joining'); // joining | media | connecting | live | ended | error
  const [attempt, setAttempt] = useState(0);
  const [info, setInfo] = useState(null);
  const [error, setError] = useState(null);
  const [endMessage, setEndMessage] = useState(null);
  const [permissionWarning, setPermissionWarning] = useState(null);
  const [muted, setMuted] = useState(false);
  const [cameraOn, setCameraOn] = useState(false);
  const [canUseCamera, setCanUseCamera] = useState(false);
  const [remotePresent, setRemotePresent] = useState(false);
  const [remoteVideo, setRemoteVideo] = useState(false);

  const localVideoRef = useRef(null);
  const remoteVideoRef = useRef(null);
  const audioRef = useRef(null);
  const callRef = useRef(null);
  const previewRef = useRef(null);

  const isDoctor = role === 'doctor';
  const counterpartName = isDoctor ? (info?.patient_name || 'Patient') : (info?.doctor_name || 'On-duty doctor');
  const counterpartRole = isDoctor ? 'patient' : 'doctor';

  useEffect(() => {
    let cancelled = false;
    let call = null;
    let preview = null;
    const guard = (fn) => (...args) => { if (!cancelled) fn(...args); };

    setPhase('joining');
    setError(null);
    setEndMessage(null);
    setPermissionWarning(null);
    setMuted(false);
    setCameraOn(false);
    setRemotePresent(false);
    setRemoteVideo(false);

    (async () => {
      if (!meetingId || !caseId) {
        setError('This video link is incomplete. Please open the full link sent on WhatsApp.');
        setPhase('error');
        return;
      }

      // A doctor link opened in a new tab has no portal session (tokens are per tab):
      // sign in first; the Hosted UI brings the doctor back to this same /call/... URL. The link
      // sent to the doctor's own WhatsApp (DOC-4) carries their token instead: no sign-in.
      if (isDoctor && !embedded && !token) {
        const user = await ensureSignedIn();
        if (!user || cancelled) return;
      }

      let joined;
      try {
        joined = await videoApi.joinMeeting(meetingId, { role, caseId, token });
      } catch (err) {
        if (!cancelled) {
          setError(describeJoinError(err, role));
          setPhase('error');
        }
        return;
      }
      if (cancelled) return;
      setInfo(joined);

      setPhase('media');
      const perms = await requestMediaPermissions();
      if (cancelled) return;
      setPermissionWarning(describePermissionProblem(perms));
      setCanUseCamera(perms.video);

      if (joined.mock) {
        // Simulated room: local preview only, nothing leaves the browser.
        preview = await openPreviewStream(perms);
        if (cancelled) { stopStream(preview); return; }
        previewRef.current = preview;
        setCameraOn(Boolean(preview?.getVideoTracks().length));
        setPhase('live');
        return;
      }

      setPhase('connecting');
      try {
        const { ChimeCall } = await import('../video/chimeCall');
        if (cancelled) return;
        call = new ChimeCall({
          meeting: joined.Meeting,
          attendee: joined.Attendee,
          handlers: {
            getLocalVideoElement: () => localVideoRef.current,
            getRemoteVideoElement: () => remoteVideoRef.current,
            getAudioElement: () => audioRef.current,
            onStarted: guard(() => setPhase('live')),
            onRemoteVideo: guard((has) => setRemoteVideo(has)),
            onRemotePresence: guard((present) => {
              setRemotePresent(present);
              if (!present) setRemoteVideo(false);
            }),
            onEnded: guard((reason, message) => {
              setEndMessage(message);
              setPhase('ended');
            }),
            onError: guard((message) => {
              setError(message);
              setPhase('error');
            })
          }
        });
        callRef.current = call;
        await call.start({ audio: perms.audio, video: perms.video });
        if (cancelled) return;
        setCameraOn(call.videoOn);
      } catch (err) {
        console.error('[VideoRoom] Chime session failed', err);
        if (!cancelled) {
          setError('Could not start the video call on this device. Check your connection and try again.');
          setPhase('error');
        }
      }
    })();

    return () => {
      cancelled = true;
      if (call) call.leave();
      callRef.current = null;
      stopStream(preview);
      previewRef.current = null;
    };
  }, [meetingId, caseId, role, token, attempt]);

  // Simulated room: attach the self-preview once the video element is on screen.
  useEffect(() => {
    if (phase === 'live' && info?.mock && localVideoRef.current && previewRef.current) {
      localVideoRef.current.srcObject = previewRef.current;
    }
  }, [phase, info]);

  const toggleMute = () => {
    const next = !muted;
    if (callRef.current) callRef.current.setMuted(next);
    previewRef.current?.getAudioTracks().forEach((t) => { t.enabled = !next; });
    setMuted(next);
  };

  const toggleCamera = async () => {
    const next = !cameraOn;
    try {
      if (callRef.current) await callRef.current.setCamera(next);
      previewRef.current?.getVideoTracks().forEach((t) => { t.enabled = next; });
      setCameraOn(next);
    } catch (err) {
      console.error('[VideoRoom] camera toggle failed', err);
      setPermissionWarning('The camera could not be started. Check that no other app is using it.');
    }
  };

  const hangUp = async () => {
    const call = callRef.current;
    callRef.current = null;
    if (call) await call.leave();
    stopStream(previewRef.current);
    previewRef.current = null;
    setEndMessage('You left the call.');
    setPhase('ended');
    onLeave?.();
  };

  const rejoin = () => setAttempt((n) => n + 1);

  const busy = phase === 'joining' || phase === 'media' || phase === 'connecting';
  const inCall = phase === 'live' || phase === 'connecting';
  const mock = Boolean(info?.mock);

  return (
    <div
      className={`${embedded ? 'h-full' : 'h-[100dvh]'} w-full flex flex-col bg-navy-950 text-slate-100 overflow-hidden`}
    >
      {/* Header */}
      {!embedded && (
        <header className="px-4 py-3 bg-navy-900 border-b border-white/10 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className="p-2 rounded-lg bg-brand-600 text-white shrink-0">
              <Video className="w-5 h-5" />
            </div>
            <div className="min-w-0">
              <h1 className="text-sm font-semibold text-white truncate">ResQ-AI Video Consultation</h1>
              <p className="text-[11px] text-slate-400 truncate">
                {isDoctor ? `Patient: ${counterpartName}` : `${counterpartName}${info?.specialty ? ` • ${info.specialty}` : ''}`}
              </p>
            </div>
          </div>
          <RoomBadge mock={mock} known={Boolean(info)} />
        </header>
      )}

      {/* Stage */}
      <div className="relative flex-1 min-h-0 bg-gradient-to-b from-navy-900 to-navy-950">
        {/* Remote participant (Chime binds the stream here) */}
        <video
          ref={remoteVideoRef}
          autoPlay
          playsInline
          className={`absolute inset-0 w-full h-full object-cover ${remoteVideo && !mock ? '' : 'invisible'}`}
        />
        <audio ref={audioRef} autoPlay className="hidden" />

        {/* Remote placeholder */}
        {inCall && !(remoteVideo && !mock) && (
          <div className="absolute inset-0 flex flex-col items-center justify-center text-center px-6">
            <div className={`w-24 h-24 sm:w-28 sm:h-28 rounded-full border-4 p-1 flex items-center justify-center ${mock ? 'border-amber-400/30' : 'border-brand-400/30'}`}>
              <div className={`w-full h-full rounded-full flex items-center justify-center text-white text-2xl font-semibold ${mock ? 'bg-amber-600' : 'bg-brand-600'}`}>
                {isDoctor ? initials(counterpartName, 'PT') : initials(counterpartName, 'DR')}
              </div>
            </div>
            <h2 className="mt-3 text-base font-semibold text-white">{counterpartName}</h2>
            {mock ? (
              <p className="mt-1 text-xs text-amber-300 max-w-xs">
                Simulated participant. Amazon Chime is not configured in this environment (mock mode),
                so no audio or video leaves this browser.
              </p>
            ) : phase === 'connecting' ? null : remotePresent ? (
              <p className="mt-1 text-xs text-slate-400">Joined, camera off</p>
            ) : (
              <p className="mt-1 text-xs text-slate-400 flex items-center gap-1.5">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                Waiting for the {counterpartRole} to join...
              </p>
            )}
          </div>
        )}

        {/* Self view */}
        {inCall && (
          <div className="absolute right-3 top-3 w-28 h-40 sm:w-48 sm:h-32 rounded-2xl overflow-hidden border border-white/15 bg-navy-800 shadow-2xl">
            <video
              ref={localVideoRef}
              autoPlay
              playsInline
              muted
              className={`w-full h-full object-cover -scale-x-100 ${cameraOn ? '' : 'invisible'}`}
            />
            {!cameraOn && (
              <div className="absolute inset-0 flex flex-col items-center justify-center text-slate-500">
                <VideoOff className="w-6 h-6" />
                <span className="text-[10px] mt-1">Camera off</span>
              </div>
            )}
            <span className="absolute bottom-1 left-1.5 text-[10px] font-semibold text-white bg-black/50 px-1.5 rounded">
              You{muted ? ' (muted)' : ''}
            </span>
          </div>
        )}

        {/* Banners */}
        <div className="absolute left-3 right-3 sm:right-auto bottom-3 flex flex-col gap-2 max-w-md">
          {embedded && info && <RoomBadge mock={mock} known />}
          {permissionWarning && inCall && (
            <div className="px-3 py-2 rounded-xl bg-amber-950/90 border border-amber-700/50 text-[11px] text-amber-200 flex gap-2">
              <AlertTriangle className="w-4 h-4 shrink-0" />
              <span>{permissionWarning}</span>
            </div>
          )}
        </div>

        {/* Busy overlay */}
        {busy && phase !== 'connecting' && (
          <CenterCard>
            <Loader2 className="w-8 h-8 text-brand-400 animate-spin" />
            <p className="mt-3 text-sm text-slate-200">{PHASE_TEXT[phase]}</p>
          </CenterCard>
        )}
        {phase === 'connecting' && (
          <div className="absolute top-3 left-3 px-3 py-1.5 rounded-lg bg-navy-900/90 border border-white/10 text-[11px] text-slate-300 flex items-center gap-2">
            <Loader2 className="w-3.5 h-3.5 animate-spin" /> {PHASE_TEXT.connecting}
          </div>
        )}

        {/* Error */}
        {phase === 'error' && (
          <CenterCard>
            <AlertTriangle className="w-9 h-9 text-red-400" />
            <p className="mt-3 text-sm text-slate-100 max-w-xs">{error}</p>
            <div className="mt-4 flex gap-2">
              <button onClick={rejoin} className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-semibold bg-white/10 hover:bg-white/20 text-white">
                <RefreshCw className="w-4 h-4" /> Try again
              </button>
              {embedded && onLeave && (
                <button onClick={onLeave} className="px-4 py-2 rounded-lg text-xs font-semibold border border-white/15 hover:bg-white/10 text-slate-300">
                  Close
                </button>
              )}
            </div>
            {!isDoctor && (
              <p className="mt-4 text-[11px] text-slate-500 max-w-xs">
                In a life-threatening emergency, call your local emergency number.
              </p>
            )}
          </CenterCard>
        )}

        {/* Ended */}
        {phase === 'ended' && (
          <CenterCard>
            <PhoneOff className="w-9 h-9 text-slate-400" />
            <p className="mt-3 text-sm text-slate-100">{endMessage || 'The call has ended.'}</p>
            <button onClick={rejoin} className="mt-4 flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-semibold bg-brand-600 hover:bg-brand-500 text-white">
              <Video className="w-4 h-4" /> Rejoin call
            </button>
          </CenterCard>
        )}
      </div>

      {/* Controls */}
      <div className="px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] bg-navy-950 border-t border-white/10 flex items-center justify-center gap-4">
        <ControlButton
          onClick={toggleMute}
          disabled={!inCall}
          active={!muted}
          label={muted ? 'Unmute' : 'Mute'}
          icon={muted ? <MicOff className="w-5 h-5" /> : <Mic className="w-5 h-5" />}
        />
        <ControlButton
          onClick={toggleCamera}
          disabled={!inCall || !canUseCamera}
          active={cameraOn}
          label={cameraOn ? 'Turn camera off' : 'Turn camera on'}
          icon={cameraOn ? <Video className="w-5 h-5" /> : <VideoOff className="w-5 h-5" />}
        />
        <button
          onClick={hangUp}
          disabled={!inCall}
          aria-label="Hang up"
          className="px-6 py-3.5 rounded-full bg-red-600 hover:bg-red-700 disabled:opacity-40 disabled:hover:bg-red-600 text-white font-semibold text-xs flex items-center gap-2 shadow-lg shadow-red-950/40 transition-all"
        >
          <PhoneOff className="w-5 h-5" />
          <span>{isDoctor ? 'End call' : 'Hang up'}</span>
        </button>
      </div>
    </div>
  );
}

function RoomBadge({ mock, known }) {
  if (!known) return null;
  return mock ? (
    <span className="self-start shrink-0 text-[10px] px-2 py-1 rounded-full bg-amber-500/15 text-amber-300 border border-amber-400/30 font-semibold flex items-center gap-1">
      <FlaskConical className="w-3 h-3" /> SIMULATED ROOM (mock mode)
    </span>
  ) : (
    <span className="self-start shrink-0 text-[10px] px-2 py-1 rounded-full bg-accent-500/15 text-accent-200 border border-accent-400/30 font-semibold flex items-center gap-1">
      <ShieldCheck className="w-3 h-3" /> Encrypted • Amazon Chime SDK
    </span>
  );
}

function CenterCard({ children }) {
  return (
    <div className="absolute inset-0 flex items-center justify-center p-6 bg-navy-950/80 backdrop-blur-sm">
      <div className="flex flex-col items-center text-center">{children}</div>
    </div>
  );
}

function ControlButton({ onClick, disabled, active, label, icon }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={`p-3.5 rounded-full transition-all disabled:opacity-40 ${
        active ? 'bg-white/10 text-white hover:bg-white/20' : 'bg-red-600 text-white hover:bg-red-700'
      }`}
    >
      {icon}
    </button>
  );
}

