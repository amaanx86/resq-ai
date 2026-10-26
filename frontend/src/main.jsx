import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import VideoRoom from './components/VideoRoom.jsx';
import { parseCallLocation } from './services/videoApi';
import './index.css';

// Patient and doctor video links (/call/<meetingId>?role=...&case=...&token=...) render a
// standalone, mobile-friendly video room instead of the portal (VIDEO-1). nginx and the Vite
// dev server both fall back to index.html for these paths. App (and with it the portal's
// axios auth interceptor, which a doctor's join needs) is still imported statically. The
// Chime SDK itself is a lazy chunk, loaded only when a real (non-simulated) call starts.

function CallPage() {
  const { meetingId, caseId, role, token } = parseCallLocation();
  return <VideoRoom meetingId={meetingId} caseId={caseId} role={role} token={token} />;
}

const isCallRoute = window.location.pathname.startsWith('/call/');

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    {isCallRoute ? <CallPage /> : <App />}
  </React.StrictMode>
);
