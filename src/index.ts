import Fastify from "fastify";
import { config } from "./config.js";
import { getDatabase, closeDatabase } from "./db/connection.js";
import { healthRoutes } from "./routes/health.js";
import { monitorRoutes } from "./routes/monitors.js";
import { checkRoutes } from "./routes/checks.js";
import { startScheduler, stopScheduler } from "./services/scheduler.js";

const app = Fastify({ logger: true });

app.register(healthRoutes);
app.register(monitorRoutes, { prefix: "/api" });
app.register(checkRoutes, { prefix: "/api" });

// Ensure DB is initialized before requests
app.addHook("onReady", async () => {
  getDatabase();
});

async function start(): Promise<void> {
  try {
    await app.listen({ port: config.port, host: config.host });
    startScheduler();
    app.log.info(`Pingback server running on ${config.host}:${config.port}`);
  } catch (err) {
    app.log.error(err);
    process.exit(1);
  }
}

async function shutdown(): Promise<void> {
  app.log.info("Shutting down...");
  stopScheduler();
  await app.close();
  closeDatabase();
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

start();
