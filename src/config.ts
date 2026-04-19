export const config = {
  port: parseInt(process.env.PORT || "3000", 10),
  host: process.env.HOST || "0.0.0.0",
  dbPath: process.env.DB_PATH || "pingback.db",
  defaultCheckInterval: 300, // 5 minutes in seconds
  checkTimeoutMs: 30_000,
  maxMonitorsFree: 3,
  maxMonitorsPro: 50,
  maxMonitorsBusiness: 200,
} as const;
