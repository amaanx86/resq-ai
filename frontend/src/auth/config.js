// Portal auth configuration (UI-2), read from Vite build-time env (see frontend/.env.example).
const env = import.meta.env;

const truthy = (v) => ['1', 'true', 'yes', 'on'].includes(String(v ?? '').trim().toLowerCase());

const region = env.VITE_AWS_REGION || 'us-east-1';

// VITE_COGNITO_DOMAIN may be a full URL, a host name, or just the Hosted UI domain prefix.
function resolveDomain(raw) {
  const value = String(raw || '').trim().replace(/\/+$/, '');
  if (!value) return '';
  if (/^https?:\/\//i.test(value)) return value;
  if (value.includes('.')) return `https://${value}`;
  return `https://${value}.auth.${region}.amazoncognito.com`;
}

const redirectUri =
  env.VITE_COGNITO_REDIRECT_URI ||
  (typeof window !== 'undefined' ? `${window.location.origin}/` : '/');

export const authConfig = {
  enabled: truthy(env.VITE_AUTH_ENABLED),
  domain: resolveDomain(env.VITE_COGNITO_DOMAIN),
  clientId: env.VITE_COGNITO_CLIENT_ID || '',
  redirectUri,
  // Must be one of the app client's allowed sign-out URLs. Defaults to the redirect URI's origin.
  logoutUri:
    env.VITE_COGNITO_LOGOUT_URI ||
    (() => {
      try {
        return `${new URL(redirectUri).origin}/`;
      } catch {
        return typeof window !== 'undefined' ? `${window.location.origin}/` : '/';
      }
    })(),
  region,
  userPoolId: env.VITE_COGNITO_USER_POOL_ID || '',
  scopes: env.VITE_COGNITO_SCOPES || 'openid email profile',
};

export function authConfigProblems() {
  if (!authConfig.enabled) return [];
  const missing = [];
  if (!authConfig.domain) missing.push('VITE_COGNITO_DOMAIN');
  if (!authConfig.clientId) missing.push('VITE_COGNITO_CLIENT_ID');
  return missing;
}
