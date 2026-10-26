import { useEffect, useState } from 'react';
import { api } from '../services/api';
import { authConfig } from '../auth/config';

/**
 * Resolves a viewable URL for a voice note (UI-1) or a photo (IMAGE-1).
 * `kind` is "audio" or "image"; with a `messageId` the media of that chat message, otherwise the
 * case's own. Returns {status, url}: status is "none" (no media, or the endpoint answered 404 /
 * doesn't exist yet), "loading", "ready" or "error".
 *
 * Real AWS gives a presigned S3 URL (used as-is). Floci/mock give a same-origin stream URL; with
 * auth on it is fetched with the bearer token and shown from a blob URL.
 */
export function useMediaUrl({ caseId, messageId = null, has, kind = 'audio' }) {
  const [state, setState] = useState({ status: 'none', url: null });

  useEffect(() => {
    if (!caseId || !has) {
      setState({ status: 'none', url: null });
      return undefined;
    }
    let cancelled = false;
    let objectUrl = null;
    setState({ status: 'loading', url: null });

    (async () => {
      try {
        const meta = kind === 'image'
          ? (messageId ? await api.getMessageImage(caseId, messageId) : await api.getCaseImage(caseId))
          : (messageId ? await api.getMessageAudio(caseId, messageId) : await api.getCaseAudio(caseId));
        let url = meta?.url;
        if (!url) throw Object.assign(new Error('no url'), { response: { status: 404 } });
        const sameOrigin = url.startsWith('/') && !url.startsWith('//');
        if (sameOrigin && authConfig.enabled) {
          const blob = await api.fetchAudioBlob(url);
          objectUrl = URL.createObjectURL(blob);
          url = objectUrl;
        }
        if (cancelled) {
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          return;
        }
        setState({ status: 'ready', url });
      } catch (err) {
        if (cancelled) return;
        const code = err?.response?.status;
        setState({ status: code === 404 || code === 405 ? 'none' : 'error', url: null });
      }
    })();

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [caseId, messageId, has, kind]);

  return state;
}

/** Voice note URL (UI-1): `kind` "case" or "message" through `messageId`. */
export function useAudioUrl({ caseId, messageId = null, hasAudio }) {
  return useMediaUrl({ caseId, messageId, has: hasAudio, kind: 'audio' });
}
