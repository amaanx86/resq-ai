// Builds portal WebSocket URLs: wss on https, and ?token=<id token> when auth is on.
import { authConfig } from './config';
import { getValidIdToken } from './authService';

export async function buildWsUrl(path) {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const url = new URL(`${proto}://${window.location.host}${path}`);
  if (authConfig.enabled) {
    const token = await getValidIdToken();
    if (!token) return null; // not signed in: don't even try
    url.searchParams.set('token', token);
  }
  return url.toString();
}
