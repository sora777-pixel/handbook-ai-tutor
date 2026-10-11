"use client";

import { useEffect } from "react";
import { apiBase } from "@/lib/api";

/**
 * Tell the local launcher that a browser UI is still open.
 *
 * start.bat spawns scripts/session_watchdog.ps1, which stops the backend and
 * the frontend once this heartbeat goes quiet -- that is what makes closing
 * the browser shut the whole app down with no separate stop script. Probing port
 * 3000 cannot do this: `next start` keeps serving long after the last tab is
 * closed, so the page has to speak up itself.
 *
 * Nothing here is user data; a failed beat is ignored on purpose (the backend
 * may still be booting, or the user may be offline from it).
 */
const BEAT_INTERVAL_MS = 15_000;

export function SessionHeartbeat() {
  useEffect(() => {
    const url = `${apiBase}/api/v1/system/heartbeat`;
    const beat = () => {
      fetch(url, {
        method: "POST",
        cache: "no-store",
        keepalive: true,
      }).catch(() => undefined);
    };

    beat();
    const timer = window.setInterval(beat, BEAT_INTERVAL_MS);
    // A tab restored from the back/forward cache may have missed several
    // intervals; beat immediately so an idle app is not reaped while in use.
    const onVisible = () => {
      if (document.visibilityState === "visible") beat();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  return null;
}