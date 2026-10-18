import axios from "axios";
import Constants from "expo-constants";
import * as SecureStore from "expo-secure-store";

import type {
  AskAnswer,
  EntityDetail,
  KnowledgeGroup,
  KnowledgeRow,
  Capture,
  CaptureDetail,
  Correction,
  Paginated,
  PipelineHealth,
  Suggestion,
  TeachChange,
  TeachResult,
  Teaching,
  Word,
  WordKind,
} from "./types";

// The server is chosen on the login screen and kept on the phone, so one
// APK works against a laptop on the same Wi-Fi today and the real server
// later. app.json's "extra.apiBase" is only the pre-filled default.
const DEFAULT_API_BASE =
  (Constants.expoConfig?.extra as { apiBase?: string } | undefined)?.apiBase ??
  "http://10.0.2.2:8000/api";

const ACCESS_KEY = "sm_access";
const REFRESH_KEY = "sm_refresh";
const SERVER_KEY = "sm_server";

export const client = axios.create({ baseURL: DEFAULT_API_BASE });

// Loaded once per JS context. The background drain runs in its own
// context with no App mounted, so this can't rely on App.tsx having run.
let serverLoaded: Promise<void> | null = null;
function ensureServerLoaded(): Promise<void> {
  serverLoaded ??= SecureStore.getItemAsync(SERVER_KEY)
    .then((stored) => {
      if (stored) client.defaults.baseURL = stored;
    })
    .catch(() => {});
  return serverLoaded;
}

/** "192.168.1.5:8000" -> "http://192.168.1.5:8000/api";
 *  "sm.example.com" -> "https://sm.example.com/api". Bare IPs and
 *  localhost get http (a dev server on the LAN); names get https. */
export function normalizeServer(input: string): string {
  let url = input.trim().replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(url)) {
    const host = url.split(/[:/]/)[0];
    const local = /^(\d{1,3}\.){3}\d{1,3}$/.test(host) || host === "localhost";
    url = `${local ? "http" : "https"}://${url}`;
  }
  if (!/\/api$/.test(url)) url += "/api";
  return url;
}

export async function getServer(): Promise<string> {
  await ensureServerLoaded();
  return client.defaults.baseURL ?? DEFAULT_API_BASE;
}

export async function setServer(input: string): Promise<string> {
  const url = normalizeServer(input);
  await SecureStore.setItemAsync(SERVER_KEY, url);
  client.defaults.baseURL = url;
  serverLoaded = Promise.resolve();
  return url;
}

export const tokenStorage = {
  async set(access: string, refresh: string) {
    await SecureStore.setItemAsync(ACCESS_KEY, access);
    await SecureStore.setItemAsync(REFRESH_KEY, refresh);
  },
  async setAccess(access: string) {
    await SecureStore.setItemAsync(ACCESS_KEY, access);
  },
  async clear() {
    await SecureStore.deleteItemAsync(ACCESS_KEY);
    await SecureStore.deleteItemAsync(REFRESH_KEY);
  },
};

/** Set by App.tsx so a failed refresh drops back to Login without this
 *  module importing navigation state. */
let onSessionExpired: (() => void) | null = null;
export function setOnSessionExpired(handler: () => void) {
  onSessionExpired = handler;
}

client.interceptors.request.use(async (config) => {
  // Axios merged the defaults into this config before interceptors ran,
  // so the stored server has to be applied here, not just on defaults.
  await ensureServerLoaded();
  config.baseURL = client.defaults.baseURL;
  const access = await SecureStore.getItemAsync(ACCESS_KEY);
  if (access) {
    config.headers.Authorization = `Bearer ${access}`;
  }
  return config;
});

// Deduped: the outbox drain and a timeline refresh can fire together, and
// three parallel refreshes would burn two of the rotated tokens.
let refreshInFlight: Promise<string> | null = null;

client.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;
    const isAuthRoute = original?.url?.includes("/auth/token");
    if (error.response?.status !== 401 || isAuthRoute || original?._retried) {
      throw error;
    }
    original._retried = true;

    const refresh = await SecureStore.getItemAsync(REFRESH_KEY);
    if (!refresh) {
      await tokenStorage.clear();
      onSessionExpired?.();
      throw error;
    }

    try {
      refreshInFlight ??= client
        .post("/auth/token/refresh/", { refresh })
        .then(async ({ data }) => {
          // ROTATE_REFRESH_TOKENS is on server-side, so a refresh response
          // carries a new refresh token too. Dropping it would leave the
          // device holding a blacklisted one and silently log you out a
          // day later.
          if (data.refresh) {
            await tokenStorage.set(data.access, data.refresh);
          } else {
            await tokenStorage.setAccess(data.access);
          }
          return data.access as string;
        })
        .finally(() => {
          refreshInFlight = null;
        });
      const access = await refreshInFlight;
      original.headers.Authorization = `Bearer ${access}`;
      return client(original);
    } catch {
      await tokenStorage.clear();
      onSessionExpired?.();
      throw error;
    }
  }
);

export function getErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const data = err.response?.data as Record<string, unknown> | undefined;
    if (typeof data?.detail === "string") return data.detail;
    if (data && typeof data === "object") {
      const first = Object.values(data)[0];
      if (Array.isArray(first) && typeof first[0] === "string") return first[0];
    }
    if (err.code === "ECONNABORTED") return "Timed out";
    if (!err.response) return "No connection";
    return err.message;
  }
  if (err instanceof Error) return err.message;
  return "Something went wrong";
}

// --- Auth ---

export async function login(username: string, password: string): Promise<void> {
  const { data } = await client.post("/auth/token/", { username, password });
  await tokenStorage.set(data.access, data.refresh);
}

export async function logout(): Promise<void> {
  await tokenStorage.clear();
}

export async function hasStoredSession(): Promise<boolean> {
  const [access, refresh] = await Promise.all([
    SecureStore.getItemAsync(ACCESS_KEY),
    SecureStore.getItemAsync(REFRESH_KEY),
  ]);
  return Boolean(access && refresh);
}

// --- Captures ---

export type UploadOutcome = "accepted" | "already-had-it";

/** Ships one recording.
 *
 *  The id is generated on this device, which is what makes the endpoint
 *  idempotent: if the upload succeeded but the response was lost — the
 *  normal outcome of a 4am connection dropping — retrying with the same
 *  id collapses onto the same row instead of duplicating the thought.
 *
 *  202 means new, 200 means the server already had it. Both mean "stop
 *  retrying", which is why they collapse into one success path here.
 */
export async function uploadCapture(item: {
  id: string;
  uri: string;
  captured_at: string;
  duration_seconds: number;
  device: string;
}): Promise<UploadOutcome> {
  const form = new FormData();
  form.append("id", item.id);
  form.append("captured_at", item.captured_at);
  form.append("duration_seconds", String(item.duration_seconds));
  form.append("device", item.device);
  // React Native's FormData takes a file descriptor rather than bytes, so
  // the audio never passes through JS — it streams straight off disk.
  form.append("audio", {
    uri: item.uri,
    name: `${item.id}.m4a`,
    type: "audio/m4a",
  } as unknown as Blob);

  const response = await client.post("/captures/", form, {
    headers: { "Content-Type": "multipart/form-data" },
    // Generous: a long capture on a bad connection is exactly the case
    // that must not fail, and this runs in the background anyway.
    timeout: 120_000,
  });

  return response.status === 200 ? "already-had-it" : "accepted";
}

export async function listCaptures(page = 1): Promise<Paginated<Capture>> {
  const { data } = await client.get("/captures/list/", { params: { page } });
  return data;
}

export async function getHealth(): Promise<PipelineHealth> {
  const { data } = await client.get("/captures/health/");
  return data;
}

export async function getCapture(id: string): Promise<CaptureDetail> {
  const { data } = await client.get(`/captures/${id}/`);
  return data;
}

// --- Editing ---

export async function saveEdit(
  id: string,
  text: string
): Promise<{ capture: CaptureDetail; suggestions: Suggestion[] }> {
  const { data } = await client.put(`/captures/${id}/transcript/`, { text });
  return data;
}

/** Drops your edit; the note shows Gemini's version again. */
export async function revertEdit(id: string): Promise<CaptureDetail> {
  const { data } = await client.delete(`/captures/${id}/transcript/`);
  return data.capture;
}

/** Re-runs the Gemini pass with your current vocabulary. Never touches
 *  your edit. Resolves once queued, not once done. */
export async function retranscribe(id: string): Promise<{ with_audio: boolean }> {
  const { data } = await client.post(`/captures/${id}/renormalize/`);
  return data;
}

// --- Vocabulary & corrections ---

export async function listWords(): Promise<Word[]> {
  const { data } = await client.get("/vocabulary/");
  return data;
}

export async function addWord(word: {
  title: string;
  kind: WordKind;
  misheard_as: string[];
  summary?: string;
}): Promise<Word> {
  const { data } = await client.post("/vocabulary/", word);
  return data;
}

export async function removeWord(id: string): Promise<void> {
  await client.delete(`/vocabulary/${id}/`);
}

export async function listCorrections(): Promise<Correction[]> {
  const { data } = await client.get("/corrections/");
  return data;
}

export async function addCorrections(pairs: { wrong: string; right: string }[]): Promise<Correction[]> {
  const { data } = await client.post("/corrections/", { pairs });
  return data;
}

export async function removeCorrection(id: string): Promise<void> {
  await client.delete(`/corrections/${id}/`);
}

// --- Asking ---

/** Answers from your notes and knowledge base. A few seconds: one
 *  planning call and one answering call on the server. */
export async function askQuestion(question: string, conversationId?: string | null): Promise<AskAnswer> {
  const { data } = await client.post(
    "/ask/",
    { question, conversation_id: conversationId ?? undefined },
    { timeout: 60_000 }
  );
  return data;
}

function clipForm(uri: string): FormData {
  const form = new FormData();
  form.append("audio", { uri, name: "clip.m4a", type: "audio/m4a" } as unknown as Blob);
  return form;
}

/** A question asked out loud: heard and answered in one round trip. The
 *  answer's `question` is what the server heard. A 422 means it heard
 *  nothing; any other failure may still carry `question` in its body. */
export async function askSpoken(uri: string, conversationId?: string | null): Promise<AskAnswer> {
  const form = clipForm(uri);
  if (conversationId) form.append("conversation_id", conversationId);
  const { data } = await client.post("/ask/spoken/", form, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 90_000,
  });
  return data;
}

/** What a failed request heard, if it got that far. */
export function heardInError(err: unknown): string | null {
  if (!axios.isAxiosError(err)) return null;
  const data = err.response?.data as { question?: unknown; transcript?: unknown } | undefined;
  const heard = data?.question ?? data?.transcript;
  return typeof heard === "string" ? heard : null;
}

export function heardNothing(err: unknown): boolean {
  return axios.isAxiosError(err) && err.response?.status === 422;
}

// --- Knowledge ---

/** Something to remember, said out loud. Returns what was heard and the
 *  changes it would make — the server saves nothing yet. */
export async function teach(uri: string): Promise<Teaching> {
  const { data } = await client.post("/knowledge/teach/", clipForm(uri), {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 90_000,
  });
  return data;
}

export async function applyTeaching(id: string, changes: TeachChange[]): Promise<TeachResult[]> {
  const { data } = await client.post(`/knowledge/teach/${id}/apply/`, { changes });
  return data.applied;
}

export async function listKnowledge(params: {
  q?: string;
  kind?: KnowledgeGroup | null;
  page?: number;
}): Promise<{ count: number; next: number | null; results: KnowledgeRow[] }> {
  const { data } = await client.get("/knowledge/", {
    params: { q: params.q || undefined, kind: params.kind || undefined, page: params.page ?? 1 },
  });
  return data;
}

export async function getEntity(id: string): Promise<EntityDetail> {
  const { data } = await client.get(`/knowledge/${id}/`);
  return data;
}

export async function updateEntity(
  id: string,
  patch: Partial<Pick<EntityDetail, "title" | "aliases" | "summary" | "body">>
): Promise<EntityDetail> {
  const { data } = await client.patch(`/knowledge/${id}/`, patch);
  return data;
}

/** Removes it from what Krono knows (soft on the server). */
export async function removeEntity(id: string): Promise<void> {
  await client.delete(`/knowledge/${id}/`);
}

/** "id is part of other" — e.g. a person to their company. */
export async function linkEntities(id: string, other: string): Promise<void> {
  await client.post(`/knowledge/${id}/links/${other}/`);
}

export async function unlinkEntities(id: string, other: string): Promise<void> {
  await client.delete(`/knowledge/${id}/links/${other}/`);
}
