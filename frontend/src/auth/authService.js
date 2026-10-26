// Cognito Hosted UI sign-in: OAuth 2.0 authorization code flow with PKCE (UI-2).
//
// Tokens live in sessionStorage (cleared when the tab closes). The portal sends the ID token
// as the bearer token (the backend checks token_use=id, cognito:groups, custom:doctor_id).
// With VITE_AUTH_ENABLED off, every function is a no-op and the user is a local administrator.
import { authConfig } from './config';
import { createPkcePair, decodeJwt, randomUrlSafeString } from './pkce';

const SESSION_KEY = 'resq.auth.session';
const PENDING_KEY = 'resq.auth.pending';
const REFRESH_MARGIN_MS = 60 * 1000;

export const LOCAL_USER = {
  sub: 'local-dev',
  email: 'local@resq.dev',
  name: 'Local Admin',
  groups: ['Administrators'],
  doctorId: null,
  isAdmin: true,
  isDoctor: false,
  local: true,
};

const listeners = new Set();
let lastReason = null;
let refreshPromise = null;
let initPromise = null;

function safeGet(storageKey) {
  try {
    const raw = sessionStorage.getItem(storageKey);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function safeSet(storageKey, value) {
  try {
    if (value == null) sessionStorage.removeItem(storageKey);
    else sessionStorage.setItem(storageKey, JSON.stringify(value));
  } catch {
    // storage blocked: the session then only lasts until reload
  }
}

let memorySession = null;

function readSession() {
  return memorySession || safeGet(SESSION_KEY);
}

function writeSession(session) {
  memorySession = session;
  safeSet(SESSION_KEY, session);
  notify();
}

function notify() {
  const user = getUser();
  listeners.forEach((fn) => {
    try {
      fn(user, lastReason);
    } catch {
      // listener errors must not break auth
    }
  });
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getUser() {
  if (!authConfig.enabled) return LOCAL_USER;
  const session = readSession();
  if (!session?.idToken) return null;
  const claims = decodeJwt(session.idToken) || {};
  const groups = Array.isArray(claims['cognito:groups'])
    ? claims['cognito:groups']
    : claims['cognito:groups']
    ? [claims['cognito:groups']]
    : [];
  return {
    sub: claims.sub,
    email: claims.email || null,
    name: claims.name || claims.given_name || claims.email || claims['cognito:username'] || 'Portal user',
    groups,
    doctorId: claims['custom:doctor_id'] || null,
    isAdmin: groups.includes('Administrators'),
    isDoctor: groups.includes('Doctors'),
    local: false,
  };
}

export function getLastReason() {
  return lastReason;
}

function sessionFromTokenResponse(data, previous = null) {
  const idToken = data.id_token;
  const claims = decodeJwt(idToken) || {};
  const expiresAt = claims.exp
    ? claims.exp * 1000
    : Date.now() + (Number(data.expires_in) || 3600) * 1000;
  return {
    idToken,
    accessToken: data.access_token || previous?.accessToken || null,
    // Cognito does not return a new refresh token on refresh: keep the old one.
    refreshToken: data.refresh_token || previous?.refreshToken || null,
    expiresAt,
  };
}

async function tokenRequest(params) {
  const res = await fetch(`${authConfig.domain}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: authConfig.clientId, ...params }).toString(),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.id_token) {
    throw new Error(data.error_description || data.error || `Token endpoint returned ${res.status}`);
  }
  return data;
}

/** Redirects to the Cognito Hosted UI. `returnTo` is restored after sign-in. */
export async function login(returnTo) {
  if (!authConfig.enabled) return;
  const { verifier, challenge } = await createPkcePair();
  const state = randomUrlSafeString(24);
  const target =
    returnTo || `${window.location.pathname}${window.location.search}${window.location.hash}`;
  safeSet(PENDING_KEY, { state, verifier, returnTo: target, createdAt: Date.now() });
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: authConfig.clientId,
    redirect_uri: authConfig.redirectUri,
    scope: authConfig.scopes,
    state,
    code_challenge: challenge,
    code_challenge_method: 'S256',
  });
  window.location.assign(`${authConfig.domain}/oauth2/authorize?${params.toString()}`);
}

function stripAuthParams() {
  const url = new URL(window.location.href);
  ['code', 'state', 'error', 'error_description'].forEach((k) => url.searchParams.delete(k));
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
}

async function handleRedirectCallback() {
  const params = new URLSearchParams(window.location.search);
  const code = params.get('code');
  const error = params.get('error');
  if (!code && !error) return false;

  const pending = safeGet(PENDING_KEY);
  safeSet(PENDING_KEY, null);
  stripAuthParams();

  if (error) {
    lastReason = `Sign-in failed: ${params.get('error_description') || error}`;
    return true;
  }
  if (!pending || pending.state !== params.get('state')) {
    lastReason = 'Sign-in failed: the login response did not match this browser session. Please try again.';
    return true;
  }
  try {
    const data = await tokenRequest({
      grant_type: 'authorization_code',
      code,
      redirect_uri: authConfig.redirectUri,
      code_verifier: pending.verifier,
    });
    lastReason = null;
    writeSession(sessionFromTokenResponse(data));
    const here = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    const safeReturn = typeof pending.returnTo === 'string' && /^\/(?!\/)/.test(pending.returnTo);
    if (safeReturn && pending.returnTo !== here) {
      window.location.replace(pending.returnTo);
    }
  } catch (e) {
    lastReason = `Sign-in failed: ${e.message}`;
  }
  return true;
}

/** Call once on startup: completes a Hosted UI redirect if there is one. Idempotent. */
export function initAuth() {
  if (!authConfig.enabled) return Promise.resolve(LOCAL_USER);
  if (!initPromise) {
    initPromise = (async () => {
      await handleRedirectCallback();
      const session = readSession();
      if (session && session.expiresAt - Date.now() < REFRESH_MARGIN_MS) {
        await refreshSession().catch(() => null);
      }
      return getUser();
    })();
  }
  return initPromise;
}

/** Uses the refresh token for new ID/access tokens. Single-flight. Resolves to the session or null. */
export function refreshSession() {
  if (!authConfig.enabled) return Promise.resolve(null);
  if (refreshPromise) return refreshPromise;
  const session = readSession();
  if (!session?.refreshToken) return Promise.resolve(null);
  refreshPromise = tokenRequest({ grant_type: 'refresh_token', refresh_token: session.refreshToken })
    .then((data) => {
      const next = sessionFromTokenResponse(data, session);
      writeSession(next);
      return next;
    })
    .catch(() => {
      expireSession('Your session has expired. Please sign in again.');
      return null;
    })
    .finally(() => {
      refreshPromise = null;
    });
  return refreshPromise;
}

/** A non-expired ID token (refreshing it if it is about to expire), or null. */
export async function getValidIdToken() {
  if (!authConfig.enabled) return null;
  const session = readSession();
  if (!session?.idToken) return null;
  if (session.expiresAt - Date.now() > REFRESH_MARGIN_MS) return session.idToken;
  const next = await refreshSession();
  return next?.idToken || null;
}

/** Drops the local session (e.g. the API answered 401) and tells the UI to show the login screen. */
export function expireSession(reason = 'Your session has expired. Please sign in again.') {
  lastReason = reason;
  memorySession = null;
  safeSet(SESSION_KEY, null);
  notify();
}

/** For pages outside the main portal (e.g. the doctor's video room link): sign in if needed. */
export async function ensureSignedIn() {
  if (!authConfig.enabled) return LOCAL_USER;
  await initAuth();
  const token = await getValidIdToken();
  if (!token) {
    await login();
    return null;
  }
  return getUser();
}

export function logout() {
  memorySession = null;
  safeSet(SESSION_KEY, null);
  safeSet(PENDING_KEY, null);
  lastReason = null;
  if (!authConfig.enabled) return;
  const params = new URLSearchParams({ client_id: authConfig.clientId, logout_uri: authConfig.logoutUri });
  window.location.assign(`${authConfig.domain}/logout?${params.toString()}`);
}
