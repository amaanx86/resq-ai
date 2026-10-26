import { useEffect, useRef, useState } from 'react';
import { buildWsUrl } from '../auth/wsUrl';

const PING_INTERVAL_MS = 25000; // below the ALB's 60 s idle timeout
const MAX_BACKOFF_MS = 30000;
// Consecutive closes before the socket ever opened (failed handshakes) after which this path is
// given up until it changes. A server refusal before accept() looks like this (1006), and so
// does a missing route or a proxy that blocks upgrades; retrying those forever only adds noise.
const MAX_HANDSHAKE_FAILURES = 6;

/**
 * Opens a portal WebSocket (`path` like "/ws/cases"), parses JSON events and hands them to
 * `onMessage`. Reconnects with exponential backoff (1 s .. 30 s, jittered), rebuilding the URL
 * each time so a refreshed ID token is used. Closes on path change / unmount.
 * Close code 1008 (auth refused / case not visible) is final: no reconnect for this path; the
 * caller's polling fallback keeps working. MAX_HANDSHAKE_FAILURES consecutive failed handshakes
 * (closed before open, e.g. 1006) are final too, until the path changes.
 * Returns true while the socket is open, so callers can poll faster when it is not.
 */
export function useReconnectingWebSocket(path, onMessage, { enabled = true } = {}) {
  const [connected, setConnected] = useState(false);
  const handlerRef = useRef(onMessage);

  useEffect(() => {
    handlerRef.current = onMessage;
  }, [onMessage]);

  useEffect(() => {
    if (!path || !enabled || typeof WebSocket === 'undefined') {
      setConnected(false);
      return undefined;
    }

    let ws = null;
    let disposed = false;
    let attempt = 0;
    let handshakeFailures = 0;
    let retryTimer = null;
    let pingTimer = null;

    const scheduleReconnect = () => {
      if (disposed) return;
      const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** attempt) + Math.random() * 500;
      attempt += 1;
      retryTimer = setTimeout(connect, delay);
    };

    async function connect() {
      if (disposed) return;
      let url = null;
      try {
        url = await buildWsUrl(path);
      } catch {
        url = null;
      }
      if (disposed) return;
      if (!url) {
        scheduleReconnect();
        return;
      }
      let opened = false;
      try {
        ws = new WebSocket(url);
      } catch {
        scheduleReconnect();
        return;
      }
      ws.onopen = () => {
        opened = true;
        attempt = 0;
        handshakeFailures = 0;
        setConnected(true);
        clearInterval(pingTimer);
        pingTimer = setInterval(() => {
          if (ws && ws.readyState === WebSocket.OPEN) {
            try {
              ws.send(JSON.stringify({ type: 'ping' }));
            } catch {
              // the close handler reconnects
            }
          }
        }, PING_INTERVAL_MS);
      };
      ws.onmessage = (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (data && typeof data === 'object') handlerRef.current?.(data);
      };
      ws.onerror = () => {
        // onclose follows and schedules the reconnect
      };
      ws.onclose = (event) => {
        clearInterval(pingTimer);
        ws = null;
        setConnected(false);
        if (event && event.code === 1008) {
          console.warn(`[ws] ${path} refused by the server (1008): not reconnecting`);
          return;
        }
        if (!opened) {
          handshakeFailures += 1;
          if (handshakeFailures >= MAX_HANDSHAKE_FAILURES) {
            console.warn(
              `[ws] ${path}: ${handshakeFailures} failed handshakes in a row (last code ${event?.code}); ` +
                'not reconnecting, polling continues'
            );
            return;
          }
        }
        scheduleReconnect();
      };
    }

    connect();

    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      clearInterval(pingTimer);
      if (ws) {
        ws.onclose = null;
        ws.onmessage = null;
        try {
          ws.close();
        } catch {
          // already closed
        }
      }
      setConnected(false);
    };
  }, [path, enabled]);

  return connected;
}
