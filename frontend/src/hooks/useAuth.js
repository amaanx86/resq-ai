import { useEffect, useState } from 'react';
import { authConfig, authConfigProblems } from '../auth/config';
import { getLastReason, getUser, initAuth, login, logout, subscribe } from '../auth/authService';

/**
 * Portal auth state. With auth disabled: ready immediately, user = local administrator.
 * Returns {ready, user, reason, enabled, configProblems, login, logout}.
 */
export function useAuth() {
  const [ready, setReady] = useState(!authConfig.enabled);
  const [user, setUser] = useState(() => (authConfig.enabled ? null : getUser()));
  const [reason, setReason] = useState(null);

  useEffect(() => {
    if (!authConfig.enabled) return undefined;
    const unsubscribe = subscribe((nextUser, nextReason) => {
      setUser(nextUser);
      setReason(nextReason);
    });
    let active = true;
    initAuth().finally(() => {
      if (!active) return;
      setUser(getUser());
      setReason(getLastReason());
      setReady(true);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  return {
    ready,
    user,
    reason,
    enabled: authConfig.enabled,
    configProblems: authConfigProblems(),
    login,
    logout,
  };
}
