import { randomUUID } from "node:crypto";
import type { FastifyInstance } from "fastify";
import { getDatabase } from "../db/connection.js";
import { MonitorRepository } from "../db/monitors.js";
import type { CreateMonitorInput } from "../types/monitor.js";

function ensureUser(userId: string): void {
  const db = getDatabase();
  db.prepare(
    "INSERT OR IGNORE INTO users (id, email) VALUES (?, ?)",
  ).run(userId, `${userId}@placeholder.local`);
}

export async function monitorRoutes(app: FastifyInstance): Promise<void> {
  app.post<{ Body: CreateMonitorInput & { userId?: string } }>("/monitors", async (request, reply) => {
    const { name, url, intervalSeconds } = request.body;
    const userId = request.body.userId || randomUUID();

    if (!name || !url) {
      return reply.status(400).send({ error: "name and url are required" });
    }

    try {
      new URL(url);
    } catch {
      return reply.status(400).send({ error: "Invalid URL" });
    }

    ensureUser(userId);
    const db = getDatabase();
    const repo = new MonitorRepository(db);
    const monitor = repo.create(userId, { name, url, intervalSeconds });
    return reply.status(201).send(monitor);
  });

  app.get<{ Params: { userId: string } }>("/users/:userId/monitors", async (request) => {
    const db = getDatabase();
    const repo = new MonitorRepository(db);
    return repo.findWithLastCheck(request.params.userId);
  });

  app.get<{ Params: { id: string } }>("/monitors/:id", async (request, reply) => {
    const db = getDatabase();
    const repo = new MonitorRepository(db);
    const monitor = repo.findById(request.params.id);
    if (!monitor) {
      return reply.status(404).send({ error: "Monitor not found" });
    }
    return monitor;
  });

  app.delete<{ Params: { id: string } }>("/monitors/:id", async (request, reply) => {
    const db = getDatabase();
    const repo = new MonitorRepository(db);
    const deleted = repo.delete(request.params.id);
    if (!deleted) {
      return reply.status(404).send({ error: "Monitor not found" });
    }
    return reply.status(204).send();
  });
}
