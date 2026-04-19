import type { FastifyInstance } from "fastify";
import { getDatabase } from "../db/connection.js";
import { MonitorRepository } from "../db/monitors.js";

export async function checkRoutes(app: FastifyInstance): Promise<void> {
  app.get<{ Params: { monitorId: string }; Querystring: { limit?: string } }>(
    "/monitors/:monitorId/checks",
    async (request, reply) => {
      const db = getDatabase();
      const repo = new MonitorRepository(db);

      const monitor = repo.findById(request.params.monitorId);
      if (!monitor) {
        return reply.status(404).send({ error: "Monitor not found" });
      }

      const limit = Math.min(
        Math.max(parseInt(request.query.limit || "100", 10) || 100, 1),
        1000,
      );

      return repo.getCheckHistory(request.params.monitorId, limit);
    },
  );

  app.get<{ Params: { monitorId: string } }>(
    "/monitors/:monitorId/checks/latest",
    async (request, reply) => {
      const db = getDatabase();
      const repo = new MonitorRepository(db);

      const monitor = repo.findById(request.params.monitorId);
      if (!monitor) {
        return reply.status(404).send({ error: "Monitor not found" });
      }

      const lastCheck = repo.getLastCheck(request.params.monitorId);
      if (!lastCheck) {
        return reply.status(404).send({ error: "No checks yet" });
      }

      return lastCheck;
    },
  );
}
