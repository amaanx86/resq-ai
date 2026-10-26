import axios from 'axios';

// Video consultation API (VIDEO-1). Uses the default axios instance so the portal's auth
// interceptor (doctor joins) applies. Patients authenticate with the token in their link.
const API_BASE = '/api/v1';

export const videoApi = {
  // -> { Meeting, Attendee, mock, case_id, role, patient_name, doctor_name, specialty }
  joinMeeting: async (meetingId, { role, caseId, token }) => {
    const params = { role, case: caseId };
    if (token) params.token = token;
    const res = await axios.post(
      `${API_BASE}/video/${encodeURIComponent(meetingId)}/join`,
      null,
      { params }
    );
    return res.data;
  }
};

// Parses /call/<meetingId>?role=patient|doctor&case=<id>&token=<t>
export function parseCallLocation(loc = window.location) {
  const match = loc.pathname.match(/^\/call\/([^/?#]+)/);
  const params = new URLSearchParams(loc.search);
  return {
    meetingId: match ? decodeURIComponent(match[1]) : null,
    role: params.get('role') === 'doctor' ? 'doctor' : 'patient',
    caseId: params.get('case'),
    token: params.get('token')
  };
}

export function describeJoinError(err, role) {
  const status = err?.response?.status;
  const detail = err?.response?.data?.detail;
  if (status === 401) {
    return role === 'doctor'
      ? 'Sign in to the ResQ-AI portal to join this call as the doctor.'
      : 'You are not authorised to join this call.';
  }
  if (status === 403) {
    return role === 'doctor'
      ? 'This case is not assigned to you.'
      : 'This video link is invalid or has expired. Please use the latest link sent on WhatsApp.';
  }
  if (status === 404) return 'This video call could not be found.';
  if (status === 410) return detail || 'This video call has ended.';
  if (typeof detail === 'string') return detail;
  return 'Could not connect to the video service. Check your connection and try again.';
}
