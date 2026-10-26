// PKCE helpers (RFC 7636) using Web Crypto. crypto.subtle needs a secure context
// (https, or http://localhost during development).

export function base64UrlEncode(bytes) {
  let binary = '';
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (let i = 0; i < view.length; i += 1) binary += String.fromCharCode(view[i]);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function randomUrlSafeString(byteLength = 48) {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

export async function createPkcePair() {
  if (!globalThis.crypto?.subtle) {
    throw new Error('Web Crypto is unavailable: the portal must be served over https (or localhost) to sign in.');
  }
  const verifier = randomUrlSafeString(64); // 86 chars, within the 43..128 limit
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return { verifier, challenge: base64UrlEncode(new Uint8Array(digest)) };
}

export function decodeJwt(token) {
  try {
    const payload = token.split('.')[1];
    const padded = payload.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(payload.length / 4) * 4, '=');
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return null;
  }
}
