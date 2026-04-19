import { config } from "../config.js";
import type { CheckStatus } from "../types/monitor.js";

export interface CheckOutcome {
  status: CheckStatus;
  statusCode: number | null;
  responseTimeMs: number | null;
  error: string | null;
}

export async function checkUrl(url: string): Promise<CheckOutcome> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.checkTimeoutMs);
  const start = Date.now();

  try {
    const response = await fetch(url, {
      method: "GET",
      signal: controller.signal,
      redirect: "follow",
      headers: { "User-Agent": "Pingback/0.1.0" },
    });

    const responseTimeMs = Date.now() - start;
    const status: CheckStatus = response.ok ? "up" : "down";

    return {
      status,
      statusCode: response.status,
      responseTimeMs,
      error: status === "down" ? `HTTP ${response.status} ${response.statusText}` : null,
    };
  } catch (err) {
    const responseTimeMs = Date.now() - start;
    const message = err instanceof Error ? err.message : String(err);

    if (message.includes("abort")) {
      return {
        status: "down",
        statusCode: null,
        responseTimeMs,
        error: `Timeout after ${config.checkTimeoutMs}ms`,
      };
    }

    return {
      status: "error",
      statusCode: null,
      responseTimeMs,
      error: message,
    };
  } finally {
    clearTimeout(timeout);
  }
}
