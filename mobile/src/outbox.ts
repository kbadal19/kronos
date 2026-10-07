/**
 * The on-device outbox.
 *
 * This is the most important module in the app. Everything else can fail
 * and be retried; a thought that never made it off the phone is simply
 * gone, and you will not remember that you had it.
 *
 * The contract: once `enqueue` returns, the thought is safe. It is a row
 * in SQLite on disk, pointing at a file on disk. The app can be killed,
 * the battery can die, the network can be absent for three days — the
 * recording still goes out. Nothing about delivery depends on a React
 * component staying mounted or a promise staying alive.
 *
 * Deletion of the local audio happens only after the server has
 * acknowledged the upload, and never before.
 */
import * as SQLite from "expo-sqlite";
import { Directory, File } from "expo-file-system";
import { Platform } from "react-native";

import { getErrorMessage, uploadCapture } from "./api";
import type { OutboxItem } from "./types";

const DB_NAME = "secondmind.db";

let dbPromise: Promise<SQLite.SQLiteDatabase> | null = null;

function db() {
  dbPromise ??= SQLite.openDatabaseAsync(DB_NAME).then(async (d) => {
    // WAL so a background drain writing a row can't block the UI thread
    // reading one.
    await d.execAsync("PRAGMA journal_mode = WAL;");
    await d.execAsync(`
      CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY NOT NULL,
        uri TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        duration_seconds REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS outbox_ready ON outbox (next_attempt_at);
    `);
    return d;
  });
  return dbPromise;
}

/** Call once on launch so the first enqueue isn't waiting on table creation. */
export async function initOutbox(): Promise<void> {
  await db();
}

export async function enqueue(item: {
  id: string;
  uri: string;
  captured_at: string;
  duration_seconds: number;
}): Promise<void> {
  const d = await db();
  await d.runAsync(
    `INSERT OR REPLACE INTO outbox
       (id, uri, captured_at, duration_seconds, attempts, next_attempt_at, last_error, created_at)
     VALUES (?, ?, ?, ?, 0, 0, NULL, ?)`,
    item.id,
    item.uri,
    item.captured_at,
    item.duration_seconds,
    Date.now()
  );
}

export async function pending(): Promise<OutboxItem[]> {
  const d = await db();
  return d.getAllAsync<OutboxItem>("SELECT * FROM outbox ORDER BY created_at ASC");
}

export async function pendingCount(): Promise<number> {
  const d = await db();
  const row = await d.getFirstAsync<{ n: number }>("SELECT COUNT(*) AS n FROM outbox");
  return row?.n ?? 0;
}

async function remove(id: string, uri: string): Promise<void> {
  const d = await db();
  await d.runAsync("DELETE FROM outbox WHERE id = ?", id);

  // Only now is the local copy expendable. If this throws, the row is
  // already gone and the file becomes garbage that sweepStrandedFiles
  // will collect — better than the reverse order, which loses audio for
  // a capture that was never accepted.
  try {
    const file = new File(uri);
    if (file.exists) file.delete();
  } catch {
    // Non-fatal by design.
  }
}

/** Exponential backoff, capped at an hour.
 *
 *  Capped rather than unbounded because the realistic failure here is a
 *  phone that was offline overnight, not a permanently broken server —
 *  and waking up to find the backoff had grown to twelve hours would mean
 *  last night's thoughts sit unsent through the whole morning. */
function backoffMs(attempts: number): number {
  return Math.min(60_000 * 2 ** attempts, 60 * 60_000);
}

let draining = false;

export type DrainResult = { sent: number; failed: number; remaining: number };

/** Ships everything that's due. Safe to call often and from anywhere —
 *  concurrent calls collapse into the one already running. */
export async function drain(): Promise<DrainResult> {
  if (draining) return { sent: 0, failed: 0, remaining: await pendingCount() };
  draining = true;

  const d = await db();
  let sent = 0;
  let failed = 0;

  try {
    const due = await d.getAllAsync<OutboxItem>(
      "SELECT * FROM outbox WHERE next_attempt_at <= ? ORDER BY created_at ASC",
      Date.now()
    );

    for (const item of due) {
      const file = new File(item.uri);
      if (!file.exists) {
        // The audio is gone — cache eviction, or a manual clear. Nothing
        // can be recovered, so stop pretending this is in flight.
        await d.runAsync("DELETE FROM outbox WHERE id = ?", item.id);
        failed += 1;
        continue;
      }

      try {
        await uploadCapture({
          id: item.id,
          uri: item.uri,
          captured_at: item.captured_at,
          duration_seconds: item.duration_seconds,
          device: `${Platform.OS} ${Platform.Version}`,
        });
        await remove(item.id, item.uri);
        sent += 1;
      } catch (err) {
        const attempts = item.attempts + 1;
        await d.runAsync(
          "UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
          attempts,
          Date.now() + backoffMs(attempts),
          getErrorMessage(err),
          item.id
        );
        failed += 1;
      }
    }
  } finally {
    draining = false;
  }

  return { sent, failed, remaining: await pendingCount() };
}

/** Deletes recordings that no outbox row points at.
 *
 *  These come from the window between the recorder writing a file and
 *  enqueue committing its row — a crash in that gap strands the audio.
 *  Rare, but the cache directory is the only place it would ever be
 *  noticed, and only as unexplained disk use. */
export async function sweepStrandedFiles(directory: string): Promise<number> {
  let removed = 0;
  try {
    const dir = new Directory(directory);
    if (!dir.exists) return 0;

    const rows = await pending();
    const known = new Set(rows.map((r) => r.uri));

    for (const entry of dir.list()) {
      if (!(entry instanceof File)) continue;
      if (known.has(entry.uri)) continue;
      // Leave anything recent alone — it may be a recording in progress.
      try {
        const info = entry.info();
        const modified = (info as { modificationTime?: number }).modificationTime;
        if (modified && Date.now() - modified * 1000 < 5 * 60_000) continue;
        entry.delete();
        removed += 1;
      } catch {
        // Skip anything we can't stat or delete.
      }
    }
  } catch {
    // Sweeping is opportunistic; never let it break a launch.
  }
  return removed;
}
