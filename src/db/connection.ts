import Database from "better-sqlite3";
import { config } from "../config.js";
import { initializeDatabase } from "./schema.js";

let db: Database.Database | null = null;

export function getDatabase(): Database.Database {
  if (!db) {
    db = new Database(config.dbPath);
    initializeDatabase(db);
  }
  return db;
}

export function closeDatabase(): void {
  if (db) {
    db.close();
    db = null;
  }
}
