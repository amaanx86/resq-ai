// Installs the portal auth interceptors on the DEFAULT axios instance, so every module that
// uses `axios` (api.js, videoApi.js) sends `Authorization: Bearer <id token>` when auth is on.
// Importing this module more than once is harmless.
import axios from 'axios';
import { authConfig } from './config';
import { expireSession, getValidIdToken, refreshSession } from './authService';

const FLAG = '__resqAuthInterceptorsInstalled';

function isApiRequest(config) {
  const url = config?.url || '';
  // Relative portal calls, or absolute calls back to our own origin. Never leak the token to
  // third parties (e.g. presigned S3 URLs).
  if (url.startsWith('/')) return !url.startsWith('//');
  try {
    return new URL(url, window.location.href).origin === window.location.origin;
  } catch {
    return false;
  }
}

function setAuthHeader(config, token) {
  if (config.headers && typeof config.headers.set === 'function') {
    config.headers.set('Authorization', `Bearer ${token}`);
  } else {
    config.headers = { ...(config.headers || {}), Authorization: `Bearer ${token}` };
  }
}

if (authConfig.enabled && !axios[FLAG]) {
  axios[FLAG] = true;

  axios.interceptors.request.use(async (config) => {
    if (!isApiRequest(config)) return config;
    const token = await getValidIdToken();
    if (token) setAuthHeader(config, token);
    return config;
  });

  axios.interceptors.response.use(
    (response) => response,
    async (error) => {
      const { config, response } = error || {};
      if (response?.status === 401 && config && isApiRequest(config)) {
        if (!config.__resqRetried) {
          config.__resqRetried = true;
          const next = await refreshSession();
          if (next?.idToken) return axios(config);
        }
        expireSession('Your session has expired or was rejected. Please sign in again.');
      }
      return Promise.reject(error);
    }
  );
}

export default axios;
