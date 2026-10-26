import axios from 'axios';
// Side effect: Cognito bearer-token interceptors on the default axios instance (no-op when auth is off).
import '../auth/axiosAuth';

const API_BASE = '/api/v1';

export const api = {
  // Cases
  getActiveCases: async () => {
    const res = await axios.get(`${API_BASE}/cases/active`);
    return res.data;
  },
  getAllCases: async () => {
    const res = await axios.get(`${API_BASE}/cases`);
    return res.data;
  },
  getCaseDetail: async (caseId) => {
    const res = await axios.get(`${API_BASE}/cases/${caseId}`);
    return res.data;
  },
  getCaseMessages: async (caseId) => {
    const res = await axios.get(`${API_BASE}/cases/${caseId}/messages`);
    return res.data;
  },
  sendDoctorMessage: async (caseId, content, senderName = "Dr. On-Duty") => {
    const res = await axios.post(`${API_BASE}/cases/${caseId}/messages`, {
      content,
      sender: "DOCTOR",
      sender_name: senderName
    });
    return res.data;
  },
  // A photo or voice note (File / Blob) to the patient's WhatsApp (IMAGE-1) -> ChatMessage.
  // caption: text under the photo, or sent just before the voice note. 413 / 415: too large or
  // not sendable; 502: WhatsApp refused it.
  sendDoctorMedia: async (caseId, file, caption = '', senderName = "Dr. On-Duty") => {
    const form = new FormData();
    form.append('file', file, file.name || 'upload');
    form.append('caption', caption);
    form.append('sender_name', senderName);
    const res = await axios.post(`${API_BASE}/cases/${caseId}/messages/media`, form);
    return res.data;
  },
  initiateVideoCall: async (caseId) => {
    const res = await axios.post(`${API_BASE}/cases/${caseId}/video-call`);
    return res.data;
  },
  // The assigned doctor calls the patient from their own WhatsApp (while calls cannot ring in the
  // portal): records it, tells the patient -> { url } (wa.me link to the patient's chat).
  startDirectCall: async (caseId) => {
    const res = await axios.post(`${API_BASE}/cases/${caseId}/direct-call`);
    return res.data;
  },
  resolveCase: async (caseId) => {
    const res = await axios.post(`${API_BASE}/cases/${caseId}/resolve`);
    return res.data;
  },
  // The doctor called an ambulance (no automatic dispatch): -> updated EmergencyCase. Idempotent.
  markAmbulanceCalled: async (caseId) => {
    const res = await axios.post(`${API_BASE}/cases/${caseId}/ambulance-called`);
    return res.data;
  },
  // Doctor case offers (OFFER-1): -> updated EmergencyCase; 409 when the offer is no longer open
  // to this doctor. doctorId only for administrators / local mode (doctors answer as themselves).
  acceptOffer: async (caseId, offerId, doctorId) => {
    const res = await axios.post(`${API_BASE}/cases/${caseId}/offer/accept`, { offer_id: offerId || null, doctor_id: doctorId || null });
    return res.data;
  },
  declineOffer: async (caseId, offerId, doctorId) => {
    const res = await axios.post(`${API_BASE}/cases/${caseId}/offer/decline`, { offer_id: offerId || null, doctor_id: doctorId || null });
    return res.data;
  },
  // Administrators: assign a doctor by hand (e.g. nobody accepted the offer). 409 when the doctor
  // already holds another case (one case per doctor).
  assignCase: async (caseId, doctorId) => {
    const res = await axios.post(`${API_BASE}/cases/${caseId}/assign`, { doctor_id: doctorId });
    return res.data;
  },
  // Administrators: "I'm on it" for a case without a doctor (ADMIN-1). Stops the alarm and the
  // calls to the administrators' phones; -> updated EmergencyCase, 409 when it is not waiting.
  acknowledgeNoDoctorAlert: async (caseId) => {
    const res = await axios.post(`${API_BASE}/cases/${caseId}/no-doctor-alert/ack`);
    return res.data;
  },

  // Administrators: delete a closed case with its chat, media and analytics. 409 for an open case.
  deleteCase: async (caseId) => {
    const res = await axios.delete(`${API_BASE}/cases/${caseId}`);
    return res.data;
  },

  // Voice note playback (UI-1): {url, expires_in}; 404 when there is no audio.
  getCaseAudio: async (caseId) => {
    const res = await axios.get(`${API_BASE}/cases/${caseId}/audio`);
    return res.data;
  },
  getMessageAudio: async (caseId, messageId) => {
    const res = await axios.get(`${API_BASE}/cases/${caseId}/messages/${messageId}/audio`);
    return res.data;
  },
  // Photos (IMAGE-1): the patient's photo on the case, or a photo in a chat message. {url, expires_in}.
  getCaseImage: async (caseId) => {
    const res = await axios.get(`${API_BASE}/cases/${caseId}/image`);
    return res.data;
  },
  getMessageImage: async (caseId, messageId) => {
    const res = await axios.get(`${API_BASE}/cases/${caseId}/messages/${messageId}/image`);
    return res.data;
  },
  // Same-origin stream URLs need the bearer token, which <audio src> / <img src> can't send: fetch as a blob.
  fetchAudioBlob: async (url) => {
    const res = await axios.get(url, { responseType: 'blob' });
    return res.data;
  },

  // Doctors
  getDoctors: async () => {
    const res = await axios.get(`${API_BASE}/doctors`);
    return res.data;
  },
  registerDoctor: async (doctorData) => {
    const res = await axios.post(`${API_BASE}/doctors/register`, doctorData);
    return res.data;
  },
  // Administrators: email the doctor's portal invitation again (temporary passwords expire), or
  // a first one with `email` for a doctor registered without one. -> DoctorProfile;
  // portal_status ACTIVE means they already set a password (no email sent).
  inviteDoctorToPortal: async (doctorId, email) => {
    const res = await axios.post(`${API_BASE}/doctors/${doctorId}/invite`, email ? { email } : {});
    return res.data;
  },
  // Administrators: edit a doctor (any of name, phone_number, license_no, specialty, bio; the email
  // stays). A new phone has to be confirmed again (phone_status PENDING); 409 while the doctor holds
  // a case or when another doctor has the number.
  updateDoctor: async (doctorId, fields) => {
    const res = await axios.patch(`${API_BASE}/doctors/${doctorId}`, fields);
    return res.data;
  },
  // Administrators: unregister a doctor (no more offers, portal access removed). 409 while they hold a case.
  unregisterDoctor: async (doctorId) => {
    const res = await axios.delete(`${API_BASE}/doctors/${doctorId}`);
    return res.data;
  },
  // Administrators: ask the doctor again to confirm their WhatsApp number (DOC-8). 409 if confirmed.
  resendNumberConfirmation: async (doctorId) => {
    const res = await axios.post(`${API_BASE}/doctors/${doctorId}/confirm-number`);
    return res.data;
  },
  // A doctor's own profile (doctors may only read their own).
  getDoctor: async (doctorId) => {
    const res = await axios.get(`${API_BASE}/doctors/${doctorId}`);
    return res.data;
  },
  toggleDoctorStatus: async (doctorId, isActive) => {
    const res = await axios.post(`${API_BASE}/doctors/${doctorId}/status?is_active=${isActive}`);
    return res.data;
  },

  // Administrators (ADMIN-2), administrators only. add: Cognito user in the Administrators group +
  // invitation email; the phone gets the no-doctor alert calls while receive_alert_calls is on.
  // 422 bad email / phone, 409 known email (or removing yourself), 502 Cognito refused.
  getAdmins: async () => {
    const res = await axios.get(`${API_BASE}/admins`);
    return res.data;
  },
  addAdmin: async (adminData) => {
    const res = await axios.post(`${API_BASE}/admins`, adminData);
    return res.data;
  },
  // fields: any of { name, phone_number, receive_alert_calls } -> AdminProfile
  updateAdmin: async (adminId, fields) => {
    const res = await axios.patch(`${API_BASE}/admins/${adminId}`, fields);
    return res.data;
  },
  inviteAdminToPortal: async (adminId) => {
    const res = await axios.post(`${API_BASE}/admins/${adminId}/invite`);
    return res.data;
  },
  removeAdmin: async (adminId) => {
    const res = await axios.delete(`${API_BASE}/admins/${adminId}`);
    return res.data;
  },

  // Analytics
  // month: optional "YYYY-MM" (backend defaults to the current UTC month); response has "month".
  getAnalytics: async (month) => {
    const res = await axios.get(`${API_BASE}/analytics/summary`, { params: month ? { month } : undefined });
    return res.data;
  },

  // Voice Note Simulation (for demo & testing)
  // -> {status: "QUEUED"|"PROCESSED", provider_message_id, case: EmergencyCase|null}
  simulateVoiceNote: async (userPhone, voiceText, language = "Urdu") => {
    const res = await axios.post(`${API_BASE}/webhook/simulate-voice`, {
      user_phone: userPhone,
      voice_text: voiceText,
      language
    });
    return res.data;
  },
  // -> {status: "PENDING"|"IN_PROGRESS"|"PROCESSED", case_id: string|null}
  getMessageStatus: async (providerMessageId) => {
    const res = await axios.get(`${API_BASE}/webhook/messages/${encodeURIComponent(providerMessageId)}`);
    return res.data;
  }
};
