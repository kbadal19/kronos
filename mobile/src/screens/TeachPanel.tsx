/**
 * Teaching SANS out loud: "Rahul is the new designer at Stracomm — he
 * handles their Instagram."
 *
 * Say it, and you get back what was heard and the changes it would make:
 * a new entry, a new summary, a name it's also called, what it belongs
 * to, a line for its notes. Untick what's wrong, then Save. Nothing is
 * written before that — a misheard name saved here would become a keyterm
 * and be pushed into every transcription after it.
 */
import Ionicons from "@expo/vector-icons/Ionicons";
import { useCallback, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { applyTeaching, getErrorMessage, heardInError, heardNothing, teach } from "../api";
import { ClipControls } from "../ClipControls";
import { colors, radius, space, type } from "../theme";
import type { TeachChange, TeachResult, Teaching } from "../types";
import { removeClip, useClipRecorder } from "../voice";
import { KIND_LABEL } from "./KnowledgeScreen";

export default function TeachPanel({
  freeMic,
  onSaved,
  onClose,
}: {
  freeMic: () => Promise<void>;
  onSaved: (results: TeachResult[]) => void;
  onClose: () => void;
}) {
  const clip = useClipRecorder();
  const [busy, setBusy] = useState<string | null>(null);
  const [teaching, setTeaching] = useState<Teaching | null>(null);
  const [kept, setKept] = useState<boolean[]>([]);
  const [heard, setHeard] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function listen() {
    setError(null);
    try {
      await freeMic();
      await clip.start();
    } catch (err) {
      setError(getErrorMessage(err));
    }
  }

  const finish = useCallback(async () => {
    const recorded = await clip.stop();
    if (!recorded) {
      setError("Too short — tap the mic, then speak");
      return;
    }
    setBusy("Working it out…");
    setTeaching(null);
    setHeard(null);
    try {
      const result = await teach(recorded.uri);
      setTeaching(result);
      setKept(result.changes.map(() => true));
    } catch (err) {
      if (heardNothing(err)) {
        setError("Didn't catch that — try again.");
      } else {
        setHeard(heardInError(err));
        setError(getErrorMessage(err));
      }
    } finally {
      removeClip(recorded.uri);
      setBusy(null);
    }
  }, [clip]);

  async function save() {
    if (!teaching) return;
    const chosen = teaching.changes.filter((_, i) => kept[i]);
    if (!chosen.length) return;
    setBusy("Saving…");
    try {
      onSaved(await applyTeaching(teaching.id, chosen));
    } catch (err) {
      setError(getErrorMessage(err));
      setBusy(null);
    }
  }

  const keptCount = kept.filter(Boolean).length;
  const reviewing = teaching !== null && !clip.recording;

  return (
    <View style={{ flex: 1 }}>
      <View style={styles.header}>
        <Text style={type.label}>TEACH SANS</Text>
        <Pressable onPress={onClose} hitSlop={12} accessibilityRole="button" accessibilityLabel="Close" disabled={clip.recording}>
          <Ionicons name="close" size={20} color={colors.textSoft} />
        </Pressable>
      </View>

      <ScrollView contentContainerStyle={styles.content}>
        {reviewing ? (
          <>
            <Text style={type.label}>YOU SAID</Text>
            <Text style={[type.body, styles.said]}>{teaching.transcript}</Text>
            {teaching.changes.length ? (
              <>
                <Text style={[type.label, { marginTop: space.lg, marginBottom: space.sm }]}>
                  SANS WILL REMEMBER — UNTICK WHAT'S WRONG
                </Text>
                {teaching.changes.map((c, i) => (
                  <ChangeCard
                    key={`${c.title}-${i}`}
                    change={c}
                    kept={kept[i]}
                    onToggle={() => setKept((k) => k.map((v, j) => (j === i ? !v : v)))}
                  />
                ))}
              </>
            ) : (
              <Text style={[type.bodySoft, { marginTop: space.lg }]}>
                Nothing to remember in that. Try naming who or what it's about.
              </Text>
            )}
          </>
        ) : (
          <Text style={[type.bodySoft, { marginTop: space.sm }]}>
            Tell SANS about a person, client, place or project, the way you'd tell a colleague —
            "Rahul is the new designer at Stracomm, he handles their Instagram." You'll see what it
            understood before anything is saved.
          </Text>
        )}
        {heard ? <Text style={[type.small, { marginTop: space.md }]}>Heard: {heard}</Text> : null}
        {error ? <Text style={[type.small, { color: colors.fail, marginTop: space.md }]}>{error}</Text> : null}
      </ScrollView>

      {reviewing && teaching.changes.length && !busy ? (
        <View style={styles.actions}>
          <Pressable
            onPress={listen}
            style={({ pressed }) => [styles.ghost, pressed && { opacity: 0.7 }]}
            accessibilityRole="button"
          >
            <Ionicons name="mic-outline" size={18} color={colors.textSoft} />
            <Text style={[type.body, { color: colors.textSoft }]}>Say again</Text>
          </Pressable>
          <Pressable
            onPress={save}
            disabled={!keptCount}
            style={({ pressed }) => [styles.save, !keptCount && { opacity: 0.4 }, pressed && { opacity: 0.7 }]}
            accessibilityRole="button"
          >
            <Text style={styles.saveText}>{keptCount ? `Save ${keptCount}` : "Nothing ticked"}</Text>
          </Pressable>
        </View>
      ) : (
        <ClipControls
          recording={clip.recording}
          seconds={clip.seconds}
          metering={clip.metering}
          busy={busy}
          idleLabel={teaching ? "Say it again" : "Tap and tell"}
          listeningHint="Listening — tap Done when you've said it"
          onStart={listen}
          onDone={finish}
          onCancel={clip.cancel}
        />
      )}
    </View>
  );
}

function ChangeCard({ change, kept, onToggle }: { change: TeachChange; kept: boolean; onToggle: () => void }) {
  const kind = (KIND_LABEL[change.kind] ?? change.kind).toUpperCase();
  return (
    <Pressable
      onPress={onToggle}
      style={({ pressed }) => [styles.card, !kept && { opacity: 0.45 }, pressed && { opacity: 0.6 }]}
      accessibilityRole="checkbox"
      accessibilityState={{ checked: kept }}
    >
      <View style={styles.cardHead}>
        <Ionicons name={kept ? "checkbox" : "square-outline"} size={20} color={kept ? colors.accent : colors.textFaint} />
        <Text style={[type.body, { flexShrink: 1, fontWeight: "600" }]} numberOfLines={1}>
          {change.title}
        </Text>
        <Text style={[type.label, change.op === "create" && { color: colors.done }]}>
          {change.op === "create" ? `NEW ${kind}` : kind}
        </Text>
      </View>
      {change.summary ? (
        <Line label={change.op === "update" && change.current_summary ? "Now" : "Who/what"} text={change.summary} />
      ) : null}
      {change.op === "update" && change.summary && change.current_summary ? (
        <Line label="Was" text={change.current_summary} faint />
      ) : null}
      {change.aliases.length ? <Line label="Also called" text={change.aliases.join(", ")} /> : null}
      {change.part_of.length ? <Line label="Part of" text={change.part_of.join(", ")} /> : null}
      {change.note ? <Line label="Note" text={change.note} /> : null}
    </Pressable>
  );
}

function Line({ label, text, faint }: { label: string; text: string; faint?: boolean }) {
  return (
    <Text style={[type.small, { marginTop: space.xs }, faint && { color: colors.textFaint }]}>
      <Text style={{ color: colors.textFaint }}>{label}: </Text>
      {text}
    </Text>
  );
}

const styles = StyleSheet.create({
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: space.lg,
    paddingTop: space.md,
  },
  content: { padding: space.lg, paddingBottom: space.xl },
  said: {
    marginTop: space.xs,
    backgroundColor: colors.surfaceHigh,
    borderRadius: radius.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
  },
  card: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.rule,
    borderRadius: radius.md,
    padding: space.md,
    marginBottom: space.sm,
  },
  cardHead: { flexDirection: "row", alignItems: "center", gap: space.sm },
  actions: { flexDirection: "row", gap: space.md, padding: space.md, borderTopWidth: 1, borderTopColor: colors.rule },
  ghost: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.xs,
    paddingVertical: space.lg,
    paddingHorizontal: space.lg,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.rule,
  },
  save: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.accent,
    borderRadius: radius.lg,
    paddingVertical: space.lg,
  },
  saveText: { ...type.title, fontSize: 16, color: colors.night },
});
