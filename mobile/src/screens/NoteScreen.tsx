/**
 * One note, every version of it, and the place you fix it.
 *
 * Three versions can exist: what Deepgram heard (never changes), what
 * Gemini made of it (re-runnable), and yours. Yours always wins, and
 * nothing on the server ever overwrites it — so fixing a note is safe
 * even if you re-transcribe it afterwards.
 *
 * Saving an edit is also how SANS learns: the words you changed come
 * back as suggestions, and the ones you keep are checked on every future
 * note. Names come pre-ticked; ordinary words don't, because "or -> aur"
 * is right in one sentence and wrong in the next.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { addCorrections, getCapture, getErrorMessage, retranscribe, revertEdit, saveEdit } from "../api";
import { formatDuration } from "../recorder";
import { colors, radius, space, type } from "../theme";
import type { CaptureDetail, Suggestion, TranscriptKind } from "../types";
import { Loading, Pill } from "../ui";

const VERSION_LABEL: Record<TranscriptKind, string> = {
  edited: "Yours",
  normalized: "SANS",
  raw: "Heard",
};
const VERSION_ORDER: TranscriptKind[] = ["edited", "normalized", "raw"];

const POLL_MS = 3000;
const POLL_LIMIT = 30; // ~90s: a re-run is one Gemini call, seconds normally.

export default function NoteScreen({
  id,
  onClose,
  onChanged,
}: {
  id: string;
  onClose: () => void;
  /** The timeline should refetch: the text it shows has changed. */
  onChanged: () => void;
}) {
  const [note, setNote] = useState<CaptureDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState<TranscriptKind | null>(null);
  const [draft, setDraft] = useState<string | null>(null); // non-null = editing
  const [busy, setBusy] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<(Suggestion & { keep: boolean })[]>([]);
  const [flash, setFlash] = useState<string | null>(null);
  const polling = useRef(false);

  const show = useCallback((next: CaptureDetail) => {
    setNote(next);
    setVersion(VERSION_ORDER.find((k) => next.transcripts.some((t) => t.kind === k)) ?? null);
  }, []);

  useEffect(() => {
    getCapture(id).then(show).catch((err) => setError(getErrorMessage(err)));
    return () => {
      polling.current = false;
    };
  }, [id, show]);

  useEffect(() => {
    if (!flash) return;
    const t = setTimeout(() => setFlash(null), 3500);
    return () => clearTimeout(t);
  }, [flash]);

  if (error && !note) {
    return (
      <Shell onClose={onClose}>
        <Text style={[type.bodySoft, { padding: space.lg }]}>Couldn't load this note — {error}</Text>
      </Shell>
    );
  }
  if (!note) {
    return (
      <Shell onClose={onClose}>
        <Loading />
      </Shell>
    );
  }

  const versions = VERSION_ORDER.filter((k) => note.transcripts.some((t) => t.kind === k));
  const current = note.transcripts.find((t) => t.kind === version);
  const when = new Date(note.captured_at);

  async function save() {
    if (draft === null || !note) return;
    const text = draft.trim();
    if (!text || text === note.text) {
      setDraft(null);
      return;
    }
    setBusy("Saving…");
    try {
      const result = await saveEdit(note.id, text);
      show(result.capture);
      setDraft(null);
      setSuggestions(result.suggestions.map((s) => ({ ...s, keep: s.recommended })));
      onChanged();
      if (!result.suggestions.length) setFlash("Saved");
    } catch (err) {
      setFlash(getErrorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function remember() {
    const pairs = suggestions.filter((s) => s.keep).map(({ wrong, right }) => ({ wrong, right }));
    setSuggestions([]);
    if (!pairs.length) return;
    try {
      await addCorrections(pairs);
      setFlash(`SANS will remember ${pairs.length} fix${pairs.length === 1 ? "" : "es"}`);
    } catch (err) {
      setFlash(getErrorMessage(err));
    }
  }

  async function undoEdit() {
    if (!note) return;
    setBusy("Restoring…");
    try {
      show(await revertEdit(note.id));
      onChanged();
      setFlash("Back to SANS's version");
    } catch (err) {
      setFlash(getErrorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function rerun() {
    if (!note) return;
    const before = note.transcripts.find((t) => t.kind === "normalized")?.id;
    setBusy("Re-transcribing…");
    try {
      await retranscribe(note.id);
      // Poll until a new normalized row appears (a re-run replaces it).
      polling.current = true;
      for (let i = 0; i < POLL_LIMIT && polling.current; i++) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        const next = await getCapture(note.id);
        const after = next.transcripts.find((t) => t.kind === "normalized")?.id;
        if (after && after !== before) {
          show(next);
          // Show what the re-run produced, even when your edit outranks it.
          setVersion("normalized");
          onChanged();
          setFlash(next.edited ? "Done — your edit is still what's shown" : "Re-transcribed");
          return;
        }
      }
      if (polling.current) setFlash("Still working — check back in a minute");
    } catch (err) {
      setFlash(getErrorMessage(err));
    } finally {
      polling.current = false;
      setBusy(null);
    }
  }

  return (
    <Shell onClose={onClose}>
      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
          <View style={styles.meta}>
            <Text style={type.small}>
              {when.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}
              {"  "}
              {when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
              {note.duration_seconds ? `  ·  ${formatDuration(note.duration_seconds)}` : ""}
            </Text>
            {note.edited ? <Pill text="Edited" tone="done" /> : null}
          </View>

          {draft === null && versions.length > 1 ? (
            <View style={styles.segment}>
              {versions.map((k) => (
                <Pressable
                  key={k}
                  onPress={() => setVersion(k)}
                  style={[styles.segmentItem, version === k && styles.segmentActive]}
                  accessibilityRole="button"
                  accessibilityState={{ selected: version === k }}
                >
                  <Text style={[type.small, version === k && { color: colors.text }]}>
                    {VERSION_LABEL[k]}
                  </Text>
                </Pressable>
              ))}
            </View>
          ) : null}

          {draft !== null ? (
            <TextInput
              value={draft}
              onChangeText={setDraft}
              multiline
              autoFocus
              textAlignVertical="top"
              style={styles.editor}
              placeholderTextColor={colors.textFaint}
              // Your own note: no suggestions bar fighting the Hinglish.
              autoCorrect={false}
              spellCheck={false}
            />
          ) : (
            <Text style={[type.body, styles.text]} selectable>
              {current?.text ?? (note.status === "failed" ? "This one couldn't be transcribed." : "Transcribing…")}
            </Text>
          )}

          {version === "raw" && draft === null ? (
            <Text style={[type.small, { marginTop: space.md }]}>
              What the recognizer heard, word for word. It never changes.
            </Text>
          ) : null}

          {suggestions.length ? (
            <View style={styles.panel}>
              <Text style={type.title}>Remember these fixes?</Text>
              <Text style={[type.small, { marginTop: space.xs, marginBottom: space.md }]}>
                Ticked ones are checked on every future note. Leave ordinary words
                unticked if they're only wrong in this sentence.
              </Text>
              {suggestions.map((s, i) => (
                <Pressable
                  key={`${s.wrong}->${s.right}`}
                  onPress={() =>
                    setSuggestions((prev) => prev.map((p, j) => (j === i ? { ...p, keep: !p.keep } : p)))
                  }
                  style={styles.suggestion}
                  accessibilityRole="checkbox"
                  accessibilityState={{ checked: s.keep }}
                >
                  <View style={[styles.box, s.keep && styles.boxOn]}>
                    {s.keep ? <Text style={styles.tick}>✓</Text> : null}
                  </View>
                  <Text style={[type.body, { flex: 1 }]}>
                    <Text style={styles.wrong}>{s.wrong}</Text>
                    {"  →  "}
                    {s.right}
                  </Text>
                </Pressable>
              ))}
              <View style={[styles.row, { marginTop: space.md }]}>
                <Button label="Skip" onPress={() => setSuggestions([])} />
                <Button
                  label={`Remember ${suggestions.filter((s) => s.keep).length || ""}`.trim()}
                  primary
                  onPress={remember}
                />
              </View>
            </View>
          ) : null}
        </ScrollView>

        {flash ? <Text style={[type.small, styles.flash]}>{flash}</Text> : null}

        <View style={styles.actions}>
          {busy ? (
            <Text style={[type.bodySoft, { flex: 1, textAlign: "center", paddingVertical: space.lg }]}>
              {busy}
            </Text>
          ) : draft !== null ? (
            <>
              <Button label="Cancel" onPress={() => setDraft(null)} />
              <Button label="Save" primary onPress={save} />
            </>
          ) : (
            <>
              {note.edited ? (
                <Button label="Undo my edit" onPress={undoEdit} />
              ) : note.transcripts.some((t) => t.kind === "raw") ? (
                <Button label="Re-transcribe" onPress={rerun} />
              ) : null}
              {note.text ? (
                <Button label="Edit" primary onPress={() => setDraft(note.text)} />
              ) : null}
            </>
          )}
        </View>
      </KeyboardAvoidingView>
    </Shell>
  );
}

function Shell({ onClose, children }: { onClose: () => void; children: React.ReactNode }) {
  return (
    <View style={{ flex: 1 }}>
      <Pressable
        onPress={onClose}
        style={({ pressed }) => [styles.back, pressed && { opacity: 0.6 }]}
        accessibilityRole="button"
        accessibilityLabel="Back"
        hitSlop={12}
      >
        <Text style={[type.body, { color: colors.accent }]}>‹ Back</Text>
      </Pressable>
      {children}
    </View>
  );
}

function Button({ label, onPress, primary }: { label: string; onPress: () => void; primary?: boolean }) {
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        primary ? styles.primary : styles.secondary,
        pressed && { opacity: 0.7 },
      ]}
      accessibilityRole="button"
    >
      <Text style={primary ? styles.primaryText : [type.body, { color: colors.textSoft }]}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  back: { paddingHorizontal: space.lg, paddingTop: space.lg, paddingBottom: space.sm, alignSelf: "flex-start" },
  content: { padding: space.lg, paddingBottom: space.xxl },
  meta: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginBottom: space.lg },
  segment: {
    flexDirection: "row",
    backgroundColor: colors.surface,
    borderRadius: radius.pill,
    padding: 3,
    marginBottom: space.lg,
    alignSelf: "flex-start",
  },
  segmentItem: { paddingHorizontal: space.lg, paddingVertical: space.sm, borderRadius: radius.pill },
  segmentActive: { backgroundColor: colors.surfaceHigh },
  text: { fontSize: 17, lineHeight: 27 },
  editor: {
    minHeight: 220,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.accent,
    borderRadius: radius.md,
    padding: space.lg,
    color: colors.text,
    fontSize: 17,
    lineHeight: 27,
  },
  panel: {
    marginTop: space.xl,
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.rule,
    padding: space.lg,
  },
  suggestion: { flexDirection: "row", alignItems: "center", gap: space.md, paddingVertical: space.sm },
  box: {
    width: 22,
    height: 22,
    borderRadius: 6,
    borderWidth: 1.5,
    borderColor: colors.textFaint,
    alignItems: "center",
    justifyContent: "center",
  },
  boxOn: { backgroundColor: colors.accent, borderColor: colors.accent },
  tick: { color: colors.night, fontSize: 14, fontWeight: "700" },
  wrong: { color: colors.textFaint, textDecorationLine: "line-through" },
  row: { flexDirection: "row", gap: space.md },
  flash: { textAlign: "center", paddingVertical: space.sm },
  actions: {
    flexDirection: "row",
    gap: space.md,
    padding: space.lg,
    borderTopWidth: 1,
    borderTopColor: colors.rule,
  },
  button: { flex: 1, borderRadius: radius.lg, paddingVertical: space.lg, alignItems: "center" },
  primary: { backgroundColor: colors.accent },
  secondary: { borderWidth: 1, borderColor: colors.rule },
  primaryText: { ...type.title, fontSize: 17, color: colors.night },
});
