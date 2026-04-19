import { randomUUID } from "node:crypto";
import type Database from "better-sqlite3";
import type { Monitor, CheckResult, CreateMonitorInput, MonitorWithLastCheck } from "../types/monitor.js";

export class MonitorRepository {
  constructor(private db: Database.Database) {}

  create(userId: string, input: CreateMonitorInput): Monitor {
    const id = randomUUID();
    const now = new Date().toISOString();
    const intervalSeconds = input.intervalSeconds ?? 300;

    this.db.prepare(`
      INSERT INTO monitors (id, user_id, name, url, interval_seconds, status, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
    `).run(id, userId, input.name, input.url, intervalSeconds, now, now);

    return {
      id,
      userId,
      name: input.name,
      url: input.url,
      intervalSeconds,
      status: "active",
      createdAt: now,
      updatedAt: now,
    };
  }

  findById(id: string): Monitor | null {
    const row = this.db.prepare(`
      SELECT id, user_id as userId, name, url, interval_seconds as intervalSeconds,
             status, created_at as createdAt, updated_at as updatedAt
      FROM monitors WHERE id = ?
    `).get(id) as Monitor | undefined;
    return row ?? null;
  }

  findByUserId(userId: string): Monitor[] {
    return this.db.prepare(`
      SELECT id, user_id as userId, name, url, interval_seconds as intervalSeconds,
             status, created_at as createdAt, updated_at as updatedAt
      FROM monitors WHERE user_id = ? ORDER BY created_at DESC
    `).all(userId) as Monitor[];
  }

  findActiveMonitors(): Monitor[] {
    return this.db.prepare(`
      SELECT id, user_id as userId, name, url, interval_seconds as intervalSeconds,
             status, created_at as createdAt, updated_at as updatedAt
      FROM monitors WHERE status = 'active'
    `).all() as Monitor[];
  }

  findWithLastCheck(userId: string): MonitorWithLastCheck[] {
    const monitors = this.findByUserId(userId);
    return monitors.map((monitor) => ({
      ...monitor,
      lastCheck: this.getLastCheck(monitor.id),
    }));
  }

  delete(id: string): boolean {
    const result = this.db.prepare("DELETE FROM monitors WHERE id = ?").run(id);
    return result.changes > 0;
  }

  saveCheckResult(monitorId: string, status: CheckResult["status"], statusCode: number | null, responseTimeMs: number | null, error: string | null): CheckResult {
    const id = randomUUID();
    const now = new Date().toISOString();

    this.db.prepare(`
      INSERT INTO check_results (id, monitor_id, status, status_code, response_time_ms, error, checked_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    `).run(id, monitorId, status, statusCode, responseTimeMs, error, now);

    return { id, monitorId, status, statusCode, responseTimeMs, error, checkedAt: now };
  }

  getLastCheck(monitorId: string): CheckResult | null {
    const row = this.db.prepare(`
      SELECT id, monitor_id as monitorId, status, status_code as statusCode,
             response_time_ms as responseTimeMs, error, checked_at as checkedAt
      FROM check_results WHERE monitor_id = ? ORDER BY checked_at DESC LIMIT 1
    `).get(monitorId) as CheckResult | undefined;
    return row ?? null;
  }

  getCheckHistory(monitorId: string, limit = 100): CheckResult[] {
    return this.db.prepare(`
      SELECT id, monitor_id as monitorId, status, status_code as statusCode,
             response_time_ms as responseTimeMs, error, checked_at as checkedAt
      FROM check_results WHERE monitor_id = ? ORDER BY checked_at DESC LIMIT ?
    `).all(monitorId, limit) as CheckResult[];
  }
}
