import { useEffect, useRef, useState } from "react";

/**
 * `/ws/ops` transport. Connect, reconnect, hand every frame straight to the
 * caller — nothing else.
 *
 * It used to also buffer frames and reveal them one every 90 ms, which meant a
 * storm of 300 events took 27 s to reach the refetch logic and the pending array
 * grew without bound (defect #26). The staggered reveal is a *ticker* concern and
 * now lives in `useRealtime`, which applies the renderer table the instant a
 * frame arrives and paces only the visible ticker.
 *
 * The connect / retry behaviour below is unchanged: a failed construction or a
 * close schedules another attempt, so a dropped WS degrades to "reconnecting"
 * and the periodic REST refresh keeps the screen populated.
 */
export function useOpsSocket(onMessage?: (data: any) => void, onOpen?: () => void) {
  const [connected, setConnected] = useState(false);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;
  // Called the moment a connection opens, before its first frame: the server replays recent
  // frames on every connect, and the caller needs to know where a connection starts to tell that
  // replay from live frames (realtime/useRealtime.ts).
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const host = window.location.host;
    const url = `${proto}://${host}/ws/ops`;
    let ws: WebSocket | null = null;
    let alive = true;
    let retry: number | undefined;

    const connect = () => {
      try {
        ws = new WebSocket(url);
      } catch {
        if (alive) retry = window.setTimeout(connect, 2500);
        return;
      }
      ws.onopen = () => {
        try {
          onOpenRef.current?.();
        } catch {
          /* the caller's bookkeeping must never stop the socket */
        }
        setConnected(true);
      };
      ws.onclose = () => {
        setConnected(false);
        if (alive) retry = window.setTimeout(connect, 2000);
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          /* ignore */
        }
      };
      ws.onmessage = (m) => {
        let data: any;
        try {
          data = JSON.parse(m.data);
        } catch {
          return; // a malformed frame is dropped, never thrown
        }
        try {
          onMessageRef.current?.(data);
        } catch {
          /* a renderer must never be able to kill the socket */
        }
      };
    };
    connect();

    return () => {
      alive = false;
      if (retry) window.clearTimeout(retry);
      ws?.close();
    };
  }, []);

  return { connected };
}
