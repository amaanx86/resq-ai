import { useCallback, useEffect, useRef, useState } from 'react';

// First format the browser can record wins: OGG/Opus (Firefox), MP4 (Safari, Chrome 126+), WebM
// (older Chrome / Edge). The server re-encodes every recording as a mono OGG/Opus WhatsApp voice note.
const MIME_CANDIDATES = [
  'audio/ogg;codecs=opus',
  'audio/mp4;codecs=mp4a.40.2',
  'audio/mp4',
  'audio/webm;codecs=opus',
  'audio/webm',
];
const EXTENSIONS = { 'audio/ogg': 'ogg', 'audio/mp4': 'm4a', 'audio/webm': 'webm' };
export const MAX_RECORDING_SECONDS = 300;

/** The format to record in, or null when the browser cannot record any of them. */
function pickMimeType() {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return null;
  return MIME_CANDIDATES.find((t) => MediaRecorder.isTypeSupported(t)) || null;
}

/**
 * Records a voice note with the microphone (IMAGE-1: the doctor's voice note to the patient).
 * start() asks for the microphone; stop() resolves `onDone(file)` with a File; cancel() drops it.
 * Returns {supported, recording, seconds, error, start, stop, cancel}.
 */
export function useVoiceRecorder(onDone) {
  const [recording, setRecording] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState(null);
  const recorderRef = useRef(null);
  const streamRef = useRef(null);
  const chunksRef = useRef([]);
  const keepRef = useRef(true);
  const timerRef = useRef(null);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  const supported = typeof window !== 'undefined' && Boolean(navigator.mediaDevices?.getUserMedia)
    && pickMimeType() !== null;

  const cleanup = useCallback(() => {
    clearInterval(timerRef.current);
    timerRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    recorderRef.current = null;
    setRecording(false);
  }, []);

  const stop = useCallback(() => {
    keepRef.current = true;
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
  }, []);

  const cancel = useCallback(() => {
    keepRef.current = false;
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
    else cleanup();
  }, [cleanup]);

  const start = useCallback(async () => {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = pickMimeType();
      const recorder = new MediaRecorder(stream, { mimeType });
      streamRef.current = stream;
      recorderRef.current = recorder;
      chunksRef.current = [];
      keepRef.current = true;
      recorder.ondataavailable = (e) => { if (e.data?.size) chunksRef.current.push(e.data); };
      recorder.onstop = () => {
        const type = recorder.mimeType || mimeType;
        const blob = new Blob(chunksRef.current, { type });
        const ext = EXTENSIONS[type.split(';')[0]] || 'm4a';
        cleanup();
        if (keepRef.current && blob.size > 0) {
          onDoneRef.current?.(new File([blob], `voice-note.${ext}`, { type }));
        }
      };
      recorder.start(250);
      setSeconds(0);
      setRecording(true);
      timerRef.current = setInterval(() => setSeconds((s) => s + 1), 1000);
    } catch (err) {
      cleanup();
      setError(err?.name === 'NotAllowedError'
        ? 'Microphone access was blocked. Allow it in the browser to record a voice note.'
        : 'Could not start recording on this device.');
    }
  }, [cleanup]);

  // A voice note is capped; it is kept, not dropped, when the cap is reached.
  useEffect(() => {
    if (recording && seconds >= MAX_RECORDING_SECONDS) stop();
  }, [recording, seconds, stop]);

  // Stop the microphone if the component goes away mid-recording.
  useEffect(() => () => {
    keepRef.current = false;
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
    clearInterval(timerRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
  }, []);

  return { supported, recording, seconds, error, start, stop, cancel };
}
