import { getDatabase } from "../db/connection.js";
import { MonitorRepository } from "../db/monitors.js";
import { checkUrl } from "./checker.js";

let intervalHandle: ReturnType<typeof setInterval> | null = null;

const TICK_INTERVAL_MS = 10_000; // check every 10 seconds which monitors are due

async function tick(): Promise<void> {
  const db = getDatabase();
  const repo = new MonitorRepository(db);
  const monitors = repo.findActiveMonitors();
  const now = Date.now();

  for (const monitor of monitors) {
    const lastCheck = repo.getLastCheck(monitor.id);

    const lastCheckTime = lastCheck
      ? new Date(lastCheck.checkedAt).getTime()
      : 0;

    const dueAt = lastCheckTime + monitor.intervalSeconds * 1000;

    if (now >= dueAt) {
      try {
        const outcome = await checkUrl(monitor.url);
        repo.saveCheckResult(
          monitor.id,
          outcome.status,
          outcome.statusCode,
          outcome.responseTimeMs,
          outcome.error,
        );
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        repo.saveCheckResult(monitor.id, "error", null, null, message);
      }
    }
  }
}

export function startScheduler(): void {
  if (intervalHandle) return;
  intervalHandle = setInterval(() => {
    tick().catch((err) => {
      console.error("Scheduler tick error:", err);
    });
  }, TICK_INTERVAL_MS);
  // Run first tick immediately
  tick().catch((err) => {
    console.error("Scheduler initial tick error:", err);
  });
}

export function stopScheduler(): void {
  if (intervalHandle) {
    clearInterval(intervalHandle);
    intervalHandle = null;
  }
}
