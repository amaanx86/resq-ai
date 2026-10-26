export const FRONT_CAMERA = { facingMode: 'user' };

// Asks for camera + microphone up front so permission problems surface as a clear UI state
// (rather than a silent Chime device failure). Falls back to audio-only.
export async function requestMediaPermissions() {
  if (!navigator.mediaDevices?.getUserMedia) {
    return { audio: false, video: false, error: 'unsupported' };
  }
  const stopAll = (stream) => stream.getTracks().forEach((t) => t.stop());
  try {
    stopAll(await navigator.mediaDevices.getUserMedia({ audio: true, video: FRONT_CAMERA }));
    return { audio: true, video: true, error: null };
  } catch (err) {
    try {
      stopAll(await navigator.mediaDevices.getUserMedia({ audio: true }));
      return { audio: true, video: false, error: err?.name || 'camera_failed' };
    } catch (err2) {
      return { audio: false, video: false, error: err2?.name || 'media_failed' };
    }
  }
}

// Live local stream for the simulated (mock) room's self-preview. Null if unavailable.
export async function openPreviewStream({ audio, video }) {
  if (!navigator.mediaDevices?.getUserMedia || (!audio && !video)) return null;
  try {
    return await navigator.mediaDevices.getUserMedia({ audio, video: video ? FRONT_CAMERA : false });
  } catch {
    return null;
  }
}

export function stopStream(stream) {
  stream?.getTracks().forEach((t) => t.stop());
}

export function describePermissionProblem(perms) {
  if (!perms || (perms.audio && perms.video)) return null;
  if (perms.error === 'unsupported') {
    return 'This browser cannot use the camera or microphone here (it needs a secure https:// page). You can still watch and listen.';
  }
  if (perms.audio && !perms.video) {
    return 'Camera unavailable or blocked. You can still talk. Allow camera access in your browser settings, then rejoin.';
  }
  return 'Microphone and camera are blocked. Allow access in your browser settings, then rejoin, so the doctor can hear you.';
}
