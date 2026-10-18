/**
 * What Krono knows.
 *
 * Two sections:
 *
 *   Knowledge    every person, company, place and project — searchable
 *                (by sound too: "starcom" finds Stracomm), filterable, and
 *                each one opens a page with its connections and every
 *                note that mentions it.
 *   Corrections  wrong -> right fixes checked on every note. "Often
 *                misheard as" lands here, as do fixes kept after editing.
 *
 * Adding is by voice first: the mic beside search opens TeachPanel, where
 * you say what to remember and approve what Krono made of it. The typed
 * form behind + stays for spelling a name exactly.
 *
 * A misheard form must never become an entity, or the recognizer gets
 * nudged towards the mistake — which is why the two are kept apart.
 */
import Ionicons from "@expo/vector-icons/Ionicons";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import {
  addCorrections,
  addWord,
  getErrorMessage,
  listCorrections,
  listKnowledge,
  removeCorrection,
} from "../api";
import { colors, radius, space, type } from "../theme";
import type { Correction, KnowledgeGroup, KnowledgeRow, WordKind } from "../types";
import { Loading } from "../ui";
import TeachPanel from "./TeachPanel";

export const KIND_LABEL: Record<string, string> = {
  person: "Person",
  company: "Company",
  project: "Project",
  place: "Place",
  technology: "Tech",
  concept: "Term",
  goal: "Goal",
  habit: "Habit",
  resource: "Resource",
};

const GROUPS: { group: KnowledgeGroup | null; label: string }[] = [
  { group: null, label: "All" },
  { group: "person", label: "People" },
  { group: "company", label: "Companies" },
  { group: "place", label: "Places" },
  { group: "project", label: "Projects" },
  { group: "other", label: "Other" },
];

const ADD_KINDS: WordKind[] = ["person", "company", "project", "place", "technology", "concept"];

function splitList(value: string) {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

export default function KnowledgeScreen({
  reloadKey,
  onOpenEntity,
  freeMic,
}: {
  reloadKey: number;
  onOpenEntity: (id: string) => void;
  /** Saves a capture still recording, so the mic is ours. */
  freeMic: () => Promise<void>;
}) {
  const [section, setSection] = useState<"knowledge" | "corrections">("knowledge");
  return (
    <View style={{ flex: 1 }}>
      <View style={styles.segment}>
        {(["knowledge", "corrections"] as const).map((s) => (
          <Pressable
            key={s}
            onPress={() => setSection(s)}
            style={[styles.segmentItem, section === s && styles.segmentActive]}
            accessibilityRole="tab"
            accessibilityState={{ selected: section === s }}
          >
            <Text style={[type.small, section === s && { color: colors.text }]}>
              {s === "knowledge" ? "Knowledge" : "Corrections"}
            </Text>
          </Pressable>
        ))}
      </View>
      {section === "knowledge" ? (
        <KnowledgeList reloadKey={reloadKey} onOpenEntity={onOpenEntity} freeMic={freeMic} />
      ) : (
        <Corrections reloadKey={reloadKey} />
      )}
    </View>
  );
}

function KnowledgeList({
  reloadKey,
  onOpenEntity,
  freeMic,
}: {
  reloadKey: number;
  onOpenEntity: (id: string) => void;
  freeMic: () => Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState<KnowledgeGroup | null>(null);
  const [rows, setRows] = useState<KnowledgeRow[] | null>(null);
  const [count, setCount] = useState(0);
  const [next, setNext] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [teaching, setTeaching] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  // Responses can arrive out of order while typing; only the latest counts.
  const requestId = useRef(0);

  const load = useCallback(async () => {
    const id = ++requestId.current;
    try {
      const page = await listKnowledge({ q: query, kind: group });
      if (id !== requestId.current) return;
      setRows(page.results);
      setCount(page.count);
      setNext(page.next);
      setError(null);
    } catch (err) {
      if (id !== requestId.current) return;
      setError(getErrorMessage(err));
      setRows((prev) => prev ?? []);
    }
  }, [query, group]);

  useEffect(() => {
    const t = setTimeout(load, query ? 250 : 0);
    return () => clearTimeout(t);
  }, [load, query, reloadKey]);

  async function loadMore() {
    if (next === null) return;
    try {
      const page = await listKnowledge({ q: query, kind: group, page: next });
      setRows((prev) => [...(prev ?? []), ...page.results]);
      setNext(page.next);
    } catch {
      // Keep what's on screen.
    }
  }

  if (teaching) {
    return (
      <TeachPanel
        freeMic={freeMic}
        onClose={() => setTeaching(false)}
        onSaved={(results) => {
          setTeaching(false);
          const saved = results.filter((r) => !r.skipped);
          const missed = results.flatMap((r) => r.unlinked ?? []);
          setNotice(
            `Remembered: ${saved.map((r) => r.title).join(", ") || "nothing"}` +
              (missed.length ? ` · couldn't find ${missed.join(", ")} to link to` : "")
          );
          setQuery("");
          load();
        }}
      />
    );
  }

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <View style={styles.searchRow}>
        <TextInput
          value={query}
          onChangeText={setQuery}
          placeholder="Search people, clients, places…"
          placeholderTextColor={colors.textFaint}
          autoCorrect={false}
          style={[styles.input, { flex: 1, marginBottom: 0 }]}
          returnKeyType="search"
        />
        <Pressable
          onPress={() => {
            setAdding(false);
            setNotice(null);
            setTeaching(true);
          }}
          style={({ pressed }) => [styles.addToggle, pressed && { opacity: 0.6 }]}
          accessibilityRole="button"
          accessibilityLabel="Teach Krono by voice"
        >
          <Ionicons name="mic" size={22} color={colors.accent} />
        </Pressable>
        <Pressable
          onPress={() => setAdding((a) => !a)}
          style={({ pressed }) => [styles.addToggle, pressed && { opacity: 0.6 }]}
          accessibilityRole="button"
          accessibilityLabel={adding ? "Close add form" : "Add to knowledge"}
        >
          <Text style={[type.title, { color: colors.accent }]}>{adding ? "×" : "+"}</Text>
        </Pressable>
      </View>

      {adding ? (
        <AddForm
          onAdded={(title) => {
            setAdding(false);
            setQuery(title);
          }}
        />
      ) : (
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.chips}
          style={{ flexGrow: 0 }}
          keyboardShouldPersistTaps="handled"
        >
          {GROUPS.map((g) => (
            <Pressable
              key={g.label}
              onPress={() => setGroup(g.group)}
              style={[styles.chip, group === g.group && styles.chipOn]}
              accessibilityRole="radio"
              accessibilityState={{ selected: group === g.group }}
            >
              <Text style={[type.small, group === g.group && { color: colors.night }]}>{g.label}</Text>
            </Pressable>
          ))}
        </ScrollView>
      )}

      {rows === null ? (
        <Loading />
      ) : (
        <FlatList
          data={rows}
          keyExtractor={(r) => r.id}
          keyboardShouldPersistTaps="handled"
          contentContainerStyle={{ paddingHorizontal: space.lg, paddingBottom: space.xxl }}
          onEndReached={loadMore}
          onEndReachedThreshold={0.4}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={async () => {
                setRefreshing(true);
                await load();
                setRefreshing(false);
              }}
              tintColor={colors.textSoft}
            />
          }
          ListHeaderComponent={
            <>
              {notice ? <Text style={[type.small, { color: colors.done, marginTop: space.sm }]}>{notice}</Text> : null}
              <Text style={[type.label, { marginVertical: space.sm }]}>
                {error ? `CAN'T REACH THE SERVER — ${error}` : `${count} ${query ? "MATCHES" : "KNOWN"}`}
              </Text>
            </>
          }
          ListEmptyComponent={
            <Text style={[type.bodySoft, { marginTop: space.lg }]}>
              {query ? `Nothing called "${query}" yet. Tap the mic to tell Krono about it.` : "Nothing here yet — tap the mic and tell Krono about someone."}
            </Text>
          }
          renderItem={({ item }) => (
            <Pressable
              onPress={() => onOpenEntity(item.id)}
              style={({ pressed }) => [styles.row, pressed && { opacity: 0.6 }]}
              accessibilityRole="button"
            >
              <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm }}>
                <Text style={[type.body, { flexShrink: 1 }]} numberOfLines={1}>
                  {item.title}
                </Text>
                <Text style={type.label}>{(KIND_LABEL[item.kind] ?? item.kind).toUpperCase()}</Text>
                {item.client_of ? <Text style={[type.label, { color: colors.done }]}>CLIENT</Text> : null}
              </View>
              {item.summary ? (
                <Text style={type.small} numberOfLines={1}>
                  {item.summary}
                </Text>
              ) : null}
            </Pressable>
          )}
        />
      )}
    </KeyboardAvoidingView>
  );
}

function AddForm({ onAdded }: { onAdded: (title: string) => void }) {
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<WordKind>("person");
  const [summary, setSummary] = useState("");
  const [misheard, setMisheard] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (busy || title.trim().length < 2) return;
    setBusy(true);
    try {
      const saved = await addWord({
        title: title.trim(),
        kind,
        summary: summary.trim(),
        misheard_as: splitList(misheard),
      });
      onAdded(saved.title);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <View style={styles.form}>
      <TextInput
        value={title}
        onChangeText={setTitle}
        placeholder="Name, e.g. Anushka"
        placeholderTextColor={colors.textFaint}
        autoCorrect={false}
        autoFocus
        style={styles.input}
      />
      <View style={[styles.chips, { paddingHorizontal: 0, flexWrap: "wrap" }]}>
        {ADD_KINDS.map((k) => (
          <Pressable
            key={k}
            onPress={() => setKind(k)}
            style={[styles.chip, kind === k && styles.chipOn]}
            accessibilityRole="radio"
            accessibilityState={{ selected: kind === k }}
          >
            <Text style={[type.small, kind === k && { color: colors.night }]}>{KIND_LABEL[k]}</Text>
          </Pressable>
        ))}
      </View>
      <TextInput
        value={summary}
        onChangeText={setSummary}
        placeholder="Who or what, in a few words (e.g. my sister)"
        placeholderTextColor={colors.textFaint}
        maxLength={160}
        style={styles.input}
      />
      <TextInput
        value={misheard}
        onChangeText={setMisheard}
        placeholder="Often misheard as (optional)"
        placeholderTextColor={colors.textFaint}
        autoCorrect={false}
        style={styles.input}
      />
      {error ? <Text style={[type.small, { color: colors.fail, marginBottom: space.sm }]}>{error}</Text> : null}
      <Pressable
        onPress={submit}
        disabled={busy}
        style={({ pressed }) => [styles.primary, (pressed || busy) && { opacity: 0.7 }]}
        accessibilityRole="button"
      >
        <Text style={styles.primaryText}>{busy ? "Adding…" : "Add"}</Text>
      </Pressable>
    </View>
  );
}

function Corrections({ reloadKey }: { reloadKey: number }) {
  const [fixes, setFixes] = useState<Correction[] | null>(null);
  const [wrong, setWrong] = useState("");
  const [right, setRight] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setFixes(await listCorrections());
      setError(null);
    } catch (err) {
      setError(getErrorMessage(err));
      setFixes((prev) => prev ?? []);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, reloadKey]);

  async function submit() {
    if (!wrong.trim() || !right.trim()) return;
    try {
      await addCorrections([{ wrong: wrong.trim(), right: right.trim() }]);
      setWrong("");
      setRight("");
      await load();
    } catch (err) {
      setError(getErrorMessage(err));
    }
  }

  if (fixes === null) return <Loading />;

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <ScrollView contentContainerStyle={{ padding: space.lg }} keyboardShouldPersistTaps="handled">
        <Text style={[type.small, { marginBottom: space.md }]}>
          Fixed on every note. Added from "Often misheard as" and from fixes you keep after editing a note.
        </Text>
        {error ? <Text style={[type.small, { color: colors.fail }]}>{error}</Text> : null}
        {fixes.map((c) => (
          <View key={c.id} style={[styles.row, { flexDirection: "row", alignItems: "center" }]}>
            <View style={{ flex: 1 }}>
              <Text style={type.body}>
                <Text style={styles.wrong}>{c.wrong}</Text>
                {"  →  "}
                {c.right}
              </Text>
              {c.hits ? <Text style={type.small}>Used {c.hits}×</Text> : null}
            </View>
            <Pressable
              onPress={() => removeCorrection(c.id).then(load).catch((e) => setError(getErrorMessage(e)))}
              hitSlop={12}
              accessibilityRole="button"
              accessibilityLabel={`Remove ${c.wrong}`}
            >
              <Text style={[type.title, { color: colors.textFaint }]}>×</Text>
            </Pressable>
          </View>
        ))}
        <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm, marginTop: space.md }}>
          <TextInput
            value={wrong}
            onChangeText={setWrong}
            placeholder="Heard"
            placeholderTextColor={colors.textFaint}
            autoCorrect={false}
            style={[styles.input, { flex: 1, marginBottom: 0 }]}
          />
          <Text style={type.bodySoft}>→</Text>
          <TextInput
            value={right}
            onChangeText={setRight}
            placeholder="Should be"
            placeholderTextColor={colors.textFaint}
            autoCorrect={false}
            onSubmitEditing={submit}
            style={[styles.input, { flex: 1, marginBottom: 0 }]}
          />
          <Pressable onPress={submit} hitSlop={8} accessibilityRole="button" accessibilityLabel="Add correction">
            <Text style={[type.title, { color: colors.accent }]}>+</Text>
          </Pressable>
        </View>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  segment: {
    flexDirection: "row",
    backgroundColor: colors.surface,
    borderRadius: radius.pill,
    padding: 3,
    margin: space.lg,
    marginBottom: space.sm,
    alignSelf: "flex-start",
  },
  segmentItem: { paddingHorizontal: space.lg, paddingVertical: space.sm, borderRadius: radius.pill },
  segmentActive: { backgroundColor: colors.surfaceHigh },
  searchRow: { flexDirection: "row", alignItems: "center", gap: space.sm, paddingHorizontal: space.lg },
  input: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.rule,
    borderRadius: radius.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    color: colors.text,
    fontSize: 15,
    marginBottom: space.md,
  },
  addToggle: { paddingHorizontal: space.sm },
  chips: { flexDirection: "row", gap: space.sm, paddingHorizontal: space.lg, paddingVertical: space.md },
  chip: {
    paddingHorizontal: space.md,
    paddingVertical: 6,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: colors.rule,
  },
  chipOn: { backgroundColor: colors.accent, borderColor: colors.accent },
  row: { paddingVertical: space.md, borderBottomWidth: 1, borderBottomColor: colors.rule, gap: 2 },
  form: { padding: space.lg, paddingBottom: space.sm },
  primary: { backgroundColor: colors.accent, borderRadius: radius.md, paddingVertical: space.md, alignItems: "center" },
  primaryText: { ...type.title, fontSize: 16, color: colors.night },
  wrong: { color: colors.textFaint, textDecorationLine: "line-through" },
});
