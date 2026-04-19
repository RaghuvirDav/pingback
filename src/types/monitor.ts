export interface Monitor {
  id: string;
  userId: string;
  name: string;
  url: string;
  intervalSeconds: number;
  status: MonitorStatus;
  createdAt: string;
  updatedAt: string;
}

export type MonitorStatus = "active" | "paused";

export interface CheckResult {
  id: string;
  monitorId: string;
  status: CheckStatus;
  statusCode: number | null;
  responseTimeMs: number | null;
  error: string | null;
  checkedAt: string;
}

export type CheckStatus = "up" | "down" | "error";

export interface CreateMonitorInput {
  name: string;
  url: string;
  intervalSeconds?: number;
}

export interface MonitorWithLastCheck extends Monitor {
  lastCheck: CheckResult | null;
}
