/**
 * Ask your knowledge base.
 *
 * Answers come only from your own notes and the people, clients and
 * places Krono knows — never the model's general knowledge — and every
 * claim from a note is cited. Tap a citation to read the note it came
 * from: an answer you can't check is an answer you can't trust.
 *
 * It's a conversation: follow-ups ("and who's their designer?") are
 * understood against the earlier turns, server-side, until New chat. The
 * chat itself lives for the session; every question is also logged on the
 * server, which is where answers get evaluated.
 *
 * Asked out loud, not typed: tap the mic, speak (Hindi, English or both),
 * tap Done. The server hears it with your vocabulary and answers in the
 * same request, and the bubble shows what it heard — so a wrong answer
 * that came from a mishearing is visible as one.
 */
import { useCallback, useRef, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { askQuestion, askSpoken, getErrorMessage, heardInError, heardNothing } from "../api";
import { ClipControls } from "../ClipControls";
import { colors, radius, space, type } from "../theme";
import type { AskAnswer } from "../types";
import { removeClip, useClipRecorder } from "../voice";

/** `question` is null while the server is still hearing it. */
type Exchange = { question: string | null; answer?: AskAnswer; error?: string };

const EXAMPLES = [
  "What's pending on the Doon website?",
  "Kis client ko call karna hai?",
  "What did I note yesterday?",
  "Who owns Rudraksh?",
];

export default function AskScreen({
  onOpenNote,
  freeMic,
}: {
  onOpenNote: (id: string) => void;
  /** Saves a capture still recording, so the mic is ours. */
  freeMic: () => Promise<void>;
}) {
  const [history, setHistory] = useState<Exchange[]>([]);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const scroll = useRef<ScrollView>(null);
  const clip = useClipRecorder();

  const settle = useCallback((patch: Partial<Exchange>) => {
    setHistory((h) => h.map((x, i) => (i === h.length - 1 ? { ...x, ...patch } : x)));
  }, []);

  function newChat() {
    if (busy || clip.recording) return;
    setHistory([]);
    setConversationId(null);
  }

  async function listen() {
    setFlash(null);
    try {
      await freeMic();
      await clip.start();
    } catch (err) {
      setFlash(getErrorMessage(err));
    }
  }

  const finish = useCallback(async () => {
    const recorded = await clip.stop();
    if (!recorded) {
      setFlash("Too short — tap the mic, then speak");
      return;
    }
    setBusy(true);
    setHistory((h) => [...h, { question: null }]);
    try {
      const answer = await askSpoken(recorded.uri, conversationId);
      setConversationId(answer.conversation_id);
      settle({ question: answer.question, answer });
    } catch (err) {
      if (heardNothing(err)) {
        setHistory((h) => h.slice(0, -1));
        setFlash("Didn't catch that — try again.");
      } else {
        settle({ question: heardInError(err) ?? "Your question", error: getErrorMessage(err) });
      }
    } finally {
      removeClip(recorded.uri);
      setBusy(false);
    }
  }, [clip, conversationId, settle]);

  /** The examples, which are already words. */
  async function send(q: string) {
    if (busy || clip.recording) return;
    setBusy(true);
    setHistory((h) => [...h, { question: q }]);
    try {
      const answer = await askQuestion(q, conversationId);
      setConversationId(answer.conversation_id);
      settle({ answer });
    } catch (err) {
      settle({ error: getErrorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <View style={{ flex: 1 }}>
      {history.length ? (
        <View style={styles.header}>
          <Text style={type.label}>CONVERSATION</Text>
          <Pressable onPress={newChat} hitSlop={12} accessibilityRole="button" disabled={busy || clip.recording}>
            <Text style={[type.body, { color: colors.accent }, (busy || clip.recording) && { opacity: 0.4 }]}>
              New chat
            </Text>
          </Pressable>
        </View>
      ) : null}
      <ScrollView
        ref={scroll}
        contentContainerStyle={styles.content}
        onContentSizeChange={() => scroll.current?.scrollToEnd({ animated: true })}
      >
        {history.length === 0 ? (
          <View style={styles.intro}>
            <Text style={type.title}>Ask</Text>
            <Text style={[type.bodySoft, { marginTop: space.xs, marginBottom: space.lg }]}>
              Tap the mic and ask — in Hindi, English or both. Answers come from your notes and the
              people and clients Krono knows, with the notes they came from.
            </Text>
            <Text style={[type.label, { marginBottom: space.sm }]}>OR TRY</Text>
            {EXAMPLES.map((e) => (
              <Pressable
                key={e}
                onPress={() => send(e)}
                style={({ pressed }) => [styles.example, pressed && { opacity: 0.6 }]}
                accessibilityRole="button"
              >
                <Text style={type.body}>{e}</Text>
              </Pressable>
            ))}
          </View>
        ) : (
          history.map((x, i) => (
            <View key={i} style={{ marginBottom: space.xl }}>
              <View style={styles.question}>
                {x.question === null ? (
                  <Text style={type.bodySoft}>Hearing you…</Text>
                ) : (
                  <Text style={type.body}>{x.question}</Text>
                )}
              </View>
              {x.answer ? (
                <Answer answer={x.answer} onOpenNote={onOpenNote} />
              ) : x.error ? (
                <Text style={[type.small, { color: colors.fail, marginTop: space.sm }]}>
                  Couldn't answer — {x.error}
                </Text>
              ) : (
                <Text style={[type.bodySoft, { marginTop: space.md }]}>Searching your notes…</Text>
              )}
            </View>
          ))
        )}
      </ScrollView>

      {flash ? <Text style={[type.small, styles.flash]}>{flash}</Text> : null}
      <ClipControls
        recording={clip.recording}
        seconds={clip.seconds}
        metering={clip.metering}
        busy={busy ? "Thinking…" : null}
        idleLabel={history.length ? "Ask a follow-up" : "Tap to ask"}
        listeningHint="Listening — tap Done when you've asked"
        onStart={listen}
        onDone={finish}
        onCancel={clip.cancel}
      />
    </View>
  );
}
function Answer({ answer, onOpenNote }: { answer: AskAnswer; onOpenNote: (id: string) => void }) {
  return (
    <View style={{ marginTop: space.md }}>
      <Formatted text={answer.answer} />
      {answer.sources.map((s) => {
        const when = new Date(s.captured_at);
        return (
          <Pressable
            key={s.capture_id}
            onPress={() => onOpenNote(s.capture_id)}
            style={({ pressed }) => [styles.source, pressed && { opacity: 0.6 }]}
            accessibilityRole="button"
            accessibilityHint="Opens the note this came from"
          >
            <Text style={[type.label, { color: colors.accent }]}>
              [{s.n}]{"  "}
              {when.toLocaleDateString(undefined, { day: "numeric", month: "short" })}{" "}
              {when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
            </Text>
            <Text style={[type.small, { marginTop: 2 }]} numberOfLines={2}>
              {s.snippet}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

/** The answers use a sliver of Markdown — **bold** and "- " bullets for
 *  overviews. Rendered here rather than pulling in a Markdown library for
 *  two constructs. */
function Formatted({ text }: { text: string }) {
  const inline = (line: string, key: string) =>
    line.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
      part.startsWith("**") && part.endsWith("**") ? (
        <Text key={`${key}-${i}`} style={{ fontWeight: "700" }}>
          {part.slice(2, -2)}
        </Text>
      ) : (
        part
      )
    );
  return (
    <View style={{ gap: space.xs }}>
      {text.split("\n").map((raw, i) => {
        const line = raw.trimEnd();
        if (!line.trim()) return <View key={i} style={{ height: space.xs }} />;
        const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
        if (bullet) {
          return (
            <View key={i} style={{ flexDirection: "row", gap: space.sm, paddingLeft: space.xs }}>
              <Text style={styles.answer}>•</Text>
              <Text style={[styles.answer, { flex: 1 }]} selectable>
                {inline(bullet[1], String(i))}
              </Text>
            </View>
          );
        }
        return (
          <Text key={i} style={styles.answer} selectable>
            {inline(line, String(i))}
          </Text>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  answer: { ...type.body, fontSize: 16, lineHeight: 25 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: space.lg,
    paddingTop: space.lg,
    paddingBottom: space.sm,
  },
  content: { padding: space.lg, paddingBottom: space.xl, flexGrow: 1 },
  intro: { flex: 1, justifyContent: "center" },
  example: {
    borderWidth: 1,
    borderColor: colors.rule,
    borderRadius: radius.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
    marginBottom: space.sm,
  },
  question: {
    alignSelf: "flex-end",
    maxWidth: "85%",
    backgroundColor: colors.surfaceHigh,
    borderRadius: radius.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
  },
  source: {
    marginTop: space.sm,
    backgroundColor: colors.surface,
    borderRadius: radius.sm,
    borderLeftWidth: 2,
    borderLeftColor: colors.accent,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  flash: { textAlign: "center", paddingHorizontal: space.lg, paddingBottom: space.xs },
});
