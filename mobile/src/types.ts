/** Mirrors the backend's capture serializers. See backend/capture/serializers.py. */

export type CaptureStatus =
  | "uploaded"
  | "transcribing"
  | "transcribed"
  | "normalized"
  | "segmented"
  | "extracted"
  | "failed";

export type Capture = {
  id: string;
  status: CaptureStatus;
  captured_at: string;
  duration_seconds: number | null;
  /** What you read: your edit, else Gemini's, else Deepgram's. */
  text: string;
  /** True when `text` is your own edit. */
  edited: boolean;
};

export type TranscriptKind = "raw" | "normalized" | "edited";

export type Transcript = {
  id: string;
  kind: TranscriptKind;
  text: string;
  provider: string;
  model: string;
  updated_at: string | null;
};

export type CaptureDetail = Capture & {
  /** Audio still on the server, so a re-run can listen again. */
  has_audio: boolean;
  error: string;
  transcripts: Transcript[];
};

/** A word swap from an edit, offered as a correction to remember. */
export type Suggestion = { wrong: string; right: string; recommended: boolean };

export type WordKind = "person" | "company" | "project" | "place" | "technology" | "concept";

/** Every semantic kind the server can hold; WordKind is what the add form offers. */
export type EntityKind = WordKind | "goal" | "habit" | "resource";

export type KnowledgeGroup = "person" | "company" | "place" | "project" | "other";

export type KnowledgeRow = {
  id: string;
  title: string;
  kind: EntityKind;
  aliases: string[];
  summary: string;
  mention_count: number;
  /** Set on clients: whose client they are. */
  client_of: string | null;
};

export type EntityLink = {
  id: string;
  title: string;
  kind: EntityKind;
  summary: string;
  /** "out": this entity is part of that one; "in": that one is part of this. */
  direction: "out" | "in";
  relation: string;
};

export type EntityMention = { capture_id: string; captured_at: string; snippet: string };

export type EntityDetail = KnowledgeRow & {
  /** Your private notes on it — used to answer questions, never in transcription prompts. */
  body: string;
  last_mentioned_at: string | null;
  links: EntityLink[];
  mention_total: number;
  mentions: EntityMention[];
};

export type Word = {
  id: string;
  title: string;
  kind: WordKind;
  aliases: string[];
  /** Who or what it is, in a line. Sent to the transcription editor. */
  summary: string;
  mention_count: number;
};

export type Correction = { id: string; wrong: string; right: string; hits: number };

export type Paginated<T> = {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
};

export type PipelineHealth = {
  total: number;
  pending: number;
  failed: number;
  awaiting_audio_cleanup: number;
};

/** A row in the on-device outbox. This is the source of truth for
 *  "has this thought been saved" — not the server, and not React state. */
export type OutboxItem = {
  id: string;
  uri: string;
  captured_at: string;
  duration_seconds: number;
  attempts: number;
  /** Epoch ms. The backoff gate: nothing is retried before this. */
  next_attempt_at: number;
  last_error: string | null;
  created_at: number;
};

export type AskSource = { n: number; capture_id: string; captured_at: string; snippet: string };

export type AskAnswer = {
  id: string;
  /** What was asked — for a spoken question, what the server heard. */
  question: string;
  /** Send back with the next question to continue the same chat. */
  conversation_id: string;
  answer: string;
  /** Only the notes the answer cites, numbered as in the text. */
  sources: AskSource[];
  entities: { id: string; title: string; kind: string; summary: string }[];
};

/** One change a spoken fact would make. Nothing is saved until applied. */
export type TeachChange = {
  op: "create" | "update";
  entity_id: string | null;
  kind: EntityKind;
  title: string;
  /** New other-names for it. */
  aliases: string[];
  /** For an update: a replacement, or null to keep the current one. */
  summary: string | null;
  current_summary: string;
  /** Added to its notes. */
  note: string | null;
  /** Titles it belongs to ("Rahul" -> "Stracomm"). */
  part_of: string[];
};

export type Teaching = { id: string; transcript: string; changes: TeachChange[] };

export type TeachResult = {
  op: "create" | "update";
  id: string;
  title: string;
  skipped?: string;
  unlinked?: string[];
};
