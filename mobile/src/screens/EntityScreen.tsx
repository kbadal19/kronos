/**
 * One entity: what SANS knows about it, your notes on it, how it
 * connects, and every note where you've mentioned it.
 *
 * Connections are tappable — walking Stracomm -> Shubham -> back is how the
 * graph gets read on a phone, without drawing it. Mentions update the
 * moment a note names it; facts change when you edit them here (and, once
 * extraction exists, when it suggests them).
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import {
  getEntity,
  getErrorMessage,
  linkEntities,
  listKnowledge,
  removeEntity,
  unlinkEntities,
  updateEntity,
} from "../api";
import { colors, radius, space, type } from "../theme";
import type { EntityDetail, KnowledgeRow } from "../types";
import { Loading, Pill } from "../ui";
import { KIND_LABEL } from "./KnowledgeScreen";

export default function EntityScreen({
  id,
  onBack,
  onOpenEntity,
  onOpenNote,
  onChanged,
}: {
  id: string;
  onBack: () => void;
  onOpenEntity: (id: string) => void;
  onOpenNote: (id: string) => void;
  /** Lists elsewhere should refetch: a name or summary changed. */
  onChanged: () => void;
}) {
  const [entity, setEntity] = useState<EntityDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [linking, setLinking] = useState(false);

  const load = useCallback(async () => {
    try {
      setEntity(await getEntity(id));
      setError(null);
    } catch (err) {
      setError(getErrorMessage(err));
    }
  }, [id]);

  useEffect(() => {
    setEntity(null);
    setEditing(false);
    setLinking(false);
    load();
  }, [load]);

  const back = (
    <Pressable
      onPress={onBack}
      style={({ pressed }) => [styles.back, pressed && { opacity: 0.6 }]}
      accessibilityRole="button"
      accessibilityLabel="Back"
      hitSlop={12}
    >
      <Text style={[type.body, { color: colors.accent }]}>‹ Back</Text>
    </Pressable>
  );

  if (!entity) {
    return (
      <View style={{ flex: 1 }}>
        {back}
        {error ? <Text style={[type.bodySoft, { padding: space.lg }]}>Couldn't load — {error}</Text> : <Loading />}
      </View>
    );
  }

  function confirmUnlink(other: { id: string; title: string }) {
    Alert.alert(`Disconnect ${other.title}?`, `${entity!.title} and ${other.title} will no longer be linked.`, [
      { text: "Cancel", style: "cancel" },
      {
        text: "Disconnect",
        style: "destructive",
        onPress: () => unlinkEntities(entity!.id, other.id).then(load).catch((e) => setError(getErrorMessage(e))),
      },
    ]);
  }

  function confirmRemove() {
    Alert.alert(
      `Remove ${entity!.title}?`,
      "SANS will stop using it for transcription and answers. Your notes that mention it stay.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Remove",
          style: "destructive",
          onPress: async () => {
            try {
              await removeEntity(entity!.id);
              onChanged();
              onBack();
            } catch (e) {
              setError(getErrorMessage(e));
            }
          },
        },
      ]
    );
  }

  const partOf = entity.links.filter((l) => l.direction === "out");
  const includes = entity.links.filter((l) => l.direction === "in");

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      {back}
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        {editing ? (
          <EditForm
            entity={entity}
            onCancel={() => setEditing(false)}
            onSaved={(next) => {
              setEntity(next);
              setEditing(false);
              onChanged();
            }}
          />
        ) : (
          <>
            <Text style={type.display} numberOfLines={3}>
              {entity.title}
            </Text>
            <View style={styles.tags}>
              <Pill text={KIND_LABEL[entity.kind] ?? entity.kind} />
              {entity.client_of ? <Pill text={`Client of ${entity.client_of}`} tone="done" /> : null}
            </View>
            {entity.aliases.length ? (
              <Text style={[type.small, { marginTop: space.sm }]}>Also called {entity.aliases.join(", ")}</Text>
            ) : null}

            <Section title="WHAT SANS KNOWS">
              <Text style={type.body}>{entity.summary || "Nothing written yet."}</Text>
            </Section>
            {entity.body ? (
              <Section title="YOUR NOTES">
                <Text style={type.body}>{entity.body}</Text>
              </Section>
            ) : null}
          </>
        )}

        {error ? <Text style={[type.small, { color: colors.fail, marginTop: space.md }]}>{error}</Text> : null}

        {!editing ? (
          <>
            <Section title={`CONNECTIONS · ${entity.links.length}`}>
              {partOf.length ? <Text style={[type.small, styles.subhead]}>Part of</Text> : null}
              {partOf.map((l) => (
                <LinkRow key={`o${l.id}`} link={l} onOpen={onOpenEntity} onLongPress={confirmUnlink} />
              ))}
              {includes.length ? <Text style={[type.small, styles.subhead]}>Includes</Text> : null}
              {includes.map((l) => (
                <LinkRow key={`i${l.id}`} link={l} onOpen={onOpenEntity} onLongPress={confirmUnlink} />
              ))}
              {linking ? (
                <LinkPicker
                  exclude={[entity.id, ...entity.links.map((l) => l.id)]}
                  onPick={async (other) => {
                    try {
                      await linkEntities(entity.id, other.id);
                      setLinking(false);
                      await load();
                    } catch (e) {
                      setError(getErrorMessage(e));
                    }
                  }}
                  onCancel={() => setLinking(false)}
                />
              ) : (
                <Pressable onPress={() => setLinking(true)} hitSlop={8} accessibilityRole="button">
                  <Text style={[type.body, { color: colors.accent, marginTop: space.sm }]}>+ Connect to…</Text>
                </Pressable>
              )}
              {entity.links.length ? (
                <Text style={[type.small, { marginTop: space.sm }]}>Long-press a connection to remove it.</Text>
              ) : null}
            </Section>

            <Section title={`MENTIONED IN ${entity.mention_total} NOTE${entity.mention_total === 1 ? "" : "S"}`}>
              {entity.mentions.length === 0 ? (
                <Text style={type.bodySoft}>Not in any note yet.</Text>
              ) : (
                entity.mentions.map((m) => {
                  const when = new Date(m.captured_at);
                  return (
                    <Pressable
                      key={m.capture_id}
                      onPress={() => onOpenNote(m.capture_id)}
                      style={({ pressed }) => [styles.mention, pressed && { opacity: 0.6 }]}
                      accessibilityRole="button"
                    >
                      <Text style={[type.label, { color: colors.accent }]}>
                        {when.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}{" "}
                        {when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
                      </Text>
                      <Text style={[type.small, { marginTop: 2 }]} numberOfLines={3}>
                        {m.snippet}
                      </Text>
                    </Pressable>
                  );
                })
              )}
            </Section>

            <View style={styles.actions}>
              <Pressable onPress={() => setEditing(true)} style={[styles.button, styles.primary]} accessibilityRole="button">
                <Text style={styles.primaryText}>Edit</Text>
              </Pressable>
              <Pressable onPress={confirmRemove} style={[styles.button, styles.secondary]} accessibilityRole="button">
                <Text style={[type.body, { color: colors.fail }]}>Remove</Text>
              </Pressable>
            </View>
          </>
        ) : null}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={{ marginTop: space.xl }}>
      <Text style={[type.label, { marginBottom: space.sm }]}>{title}</Text>
      {children}
    </View>
  );
}

function LinkRow({
  link,
  onOpen,
  onLongPress,
}: {
  link: EntityDetail["links"][number];
  onOpen: (id: string) => void;
  onLongPress: (l: { id: string; title: string }) => void;
}) {
  return (
    <Pressable
      onPress={() => onOpen(link.id)}
      onLongPress={() => onLongPress(link)}
      style={({ pressed }) => [styles.link, pressed && { opacity: 0.6 }]}
      accessibilityRole="button"
      accessibilityHint="Opens it. Long-press to disconnect."
    >
      <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
        <Text style={type.body}>{link.title}</Text>
        <Text style={type.label}>{(KIND_LABEL[link.kind] ?? link.kind).toUpperCase()}</Text>
      </View>
      {link.summary ? (
        <Text style={type.small} numberOfLines={1}>
          {link.summary}
        </Text>
      ) : null}
    </Pressable>
  );
}

function LinkPicker({
  exclude,
  onPick,
  onCancel,
}: {
  exclude: string[];
  onPick: (row: KnowledgeRow) => void;
  onCancel: () => void;
}) {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<KnowledgeRow[]>([]);
  // A string, not the array: a fresh array each render would re-run the
  // search effect on every render.
  const excludeKey = exclude.join(",");

  useEffect(() => {
    if (!q.trim()) {
      setRows([]);
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      listKnowledge({ q })
        .then((page) => live && setRows(page.results.filter((r) => !excludeKey.includes(r.id)).slice(0, 8)))
        .catch(() => {});
    }, 250);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [q, excludeKey]);

  return (
    <View style={{ marginTop: space.sm }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
        <TextInput
          value={q}
          onChangeText={setQ}
          placeholder="Connect to… (search)"
          placeholderTextColor={colors.textFaint}
          autoFocus
          autoCorrect={false}
          style={[styles.input, { flex: 1, marginBottom: 0 }]}
        />
        <Pressable onPress={onCancel} hitSlop={8} accessibilityRole="button" accessibilityLabel="Cancel">
          <Text style={[type.title, { color: colors.textFaint }]}>×</Text>
        </Pressable>
      </View>
      {rows.map((r) => (
        <Pressable key={r.id} onPress={() => onPick(r)} style={styles.link} accessibilityRole="button">
          <Text style={type.body}>{r.title}</Text>
          {r.summary ? (
            <Text style={type.small} numberOfLines={1}>
              {r.summary}
            </Text>
          ) : null}
        </Pressable>
      ))}
    </View>
  );
}

function EditForm({
  entity,
  onCancel,
  onSaved,
}: {
  entity: EntityDetail;
  onCancel: () => void;
  onSaved: (next: EntityDetail) => void;
}) {
  const [title, setTitle] = useState(entity.title);
  const [aliases, setAliases] = useState(entity.aliases.join(", "));
  const [summary, setSummary] = useState(entity.summary);
  const [body, setBody] = useState(entity.body);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    if (busy || title.trim().length < 2) return;
    setBusy(true);
    try {
      onSaved(
        await updateEntity(entity.id, {
          title: title.trim(),
          aliases: aliases
            .split(",")
            .map((a) => a.trim())
            .filter(Boolean),
          summary: summary.trim(),
          body: body.trim(),
        })
      );
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <View>
      <Field label="NAME">
        <TextInput value={title} onChangeText={setTitle} autoCorrect={false} style={styles.input} />
      </Field>
      <Field label="ALSO CALLED (comma-separated; real names only, not mishearings)">
        <TextInput value={aliases} onChangeText={setAliases} autoCorrect={false} style={styles.input} />
      </Field>
      <Field label="WHAT SANS KNOWS (one line — used when transcribing)">
        <TextInput value={summary} onChangeText={setSummary} maxLength={160} multiline style={styles.input} />
      </Field>
      <Field label="YOUR NOTES (private — used for answers, never sent while transcribing)">
        <TextInput
          value={body}
          onChangeText={setBody}
          multiline
          maxLength={4000}
          textAlignVertical="top"
          style={[styles.input, { minHeight: 120 }]}
        />
      </Field>
      {error ? <Text style={[type.small, { color: colors.fail, marginBottom: space.sm }]}>{error}</Text> : null}
      <View style={styles.actions}>
        <Pressable onPress={onCancel} style={[styles.button, styles.secondary]} accessibilityRole="button">
          <Text style={[type.body, { color: colors.textSoft }]}>Cancel</Text>
        </Pressable>
        <Pressable onPress={save} style={[styles.button, styles.primary, busy && { opacity: 0.7 }]} accessibilityRole="button">
          <Text style={styles.primaryText}>{busy ? "Saving…" : "Save"}</Text>
        </Pressable>
      </View>
    </View>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <View style={{ marginBottom: space.sm }}>
      <Text style={[type.label, { marginBottom: space.xs }]}>{label}</Text>
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  back: { paddingHorizontal: space.lg, paddingTop: space.lg, paddingBottom: space.sm, alignSelf: "flex-start" },
  content: { padding: space.lg, paddingBottom: space.xxl },
  tags: { flexDirection: "row", gap: space.sm, marginTop: space.md, flexWrap: "wrap" },
  subhead: { marginTop: space.sm, marginBottom: space.xs },
  link: { paddingVertical: space.sm, borderBottomWidth: 1, borderBottomColor: colors.rule, gap: 2 },
  mention: {
    marginBottom: space.sm,
    backgroundColor: colors.surface,
    borderRadius: radius.sm,
    borderLeftWidth: 2,
    borderLeftColor: colors.accent,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  input: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.rule,
    borderRadius: radius.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    color: colors.text,
    fontSize: 15,
    marginBottom: space.sm,
  },
  actions: { flexDirection: "row", gap: space.md, marginTop: space.xl },
  button: { flex: 1, borderRadius: radius.lg, paddingVertical: space.lg, alignItems: "center" },
  primary: { backgroundColor: colors.accent },
  secondary: { borderWidth: 1, borderColor: colors.rule },
  primaryText: { ...type.title, fontSize: 17, color: colors.night },
});
