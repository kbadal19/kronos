/**
 * Time is the spine.
 *
 * Until the graph exists this is the only way back into what you said,
 * and even afterwards it stays the thing you reach for most — a
 * reverse-chronological list needs no query language and no understanding
 * of the model underneath.
 *
 * It also shows what hasn't left the phone yet, because a queued
 * recording you can't see is indistinguishable from one that was lost.
 */
import { useCallback, useEffect, useState } from "react";
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from "react-native";

import { getErrorMessage, listCaptures } from "../api";
import { drain, pending } from "../outbox";
import { formatDuration } from "../recorder";
import { colors, radius, space, type } from "../theme";
import type { Capture, CaptureStatus, OutboxItem } from "../types";
import { Card, Empty, Loading, Pill } from "../ui";

const TONE: Record<CaptureStatus, "neutral" | "pending" | "done" | "fail"> = {
  uploaded: "pending",
  transcribing: "pending",
  transcribed: "done",
  normalized: "done",
  segmented: "done",
  extracted: "done",
  failed: "fail",
};

const LABEL: Record<CaptureStatus, string> = {
  uploaded: "Queued",
  transcribing: "Transcribing",
  transcribed: "Transcribed",
  normalized: "Transcribed",
  segmented: "Understood",
  extracted: "Linked",
  failed: "Failed",
};

export default function TimelineScreen({
  reloadKey,
  onOpen,
}: {
  reloadKey: number;
  /** Opens a note to read every version of it and edit it. */
  onOpen: (id: string) => void;
}) {
  const [items, setItems] = useState<Capture[] | null>(null);
  const [queue, setQueue] = useState<OutboxItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [nextPage, setNextPage] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      // Push before pulling, so a capture made thirty seconds ago appears
      // as a real row rather than sitting in the local queue looking stuck.
      await drain().catch(() => {});
      const [page, local] = await Promise.all([listCaptures(1), pending()]);
      setItems(page.results);
      setNextPage(page.next ? 2 : null);
      setQueue(local);
      setError(null);
    } catch (err) {
      setError(getErrorMessage(err));
      setItems((prev) => prev ?? []);
      // The local queue still renders without a network — which is the
      // moment it matters most.
      setQueue(await pending().catch(() => []));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, reloadKey]);

  const loadMore = useCallback(async () => {
    if (nextPage === null) return;
    try {
      const page = await listCaptures(nextPage);
      setItems((prev) => [...(prev ?? []), ...page.results]);
      setNextPage(page.next ? nextPage + 1 : null);
    } catch {
      // Leave what's already on screen alone.
    }
  }, [nextPage]);

  if (items === null) return <Loading label="Loading your thoughts" />;

  const empty = items.length === 0 && queue.length === 0;
  if (empty) {
    return (
      <Empty
        title="Nothing yet"
        body={
          error
            ? `Can't reach the server — ${error}`
            : "Whatever you say gets transcribed and shows up here."
        }
      />
    );
  }

  return (
    <FlatList
      data={items}
      keyExtractor={(item) => item.id}
      contentContainerStyle={{ padding: space.lg, gap: space.md }}
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
      onEndReached={loadMore}
      onEndReachedThreshold={0.4}
      ListHeaderComponent={
        <View style={{ gap: space.md }}>
          {error ? (
            <View style={styles.banner}>
              <Text style={[type.small, { color: colors.pending }]}>
                Offline — showing what's on this phone. {error}
              </Text>
            </View>
          ) : null}
          {queue.map((item) => (
            <Card key={item.id} style={styles.queued}>
              <View style={styles.row}>
                <Pill text="On this phone" tone="pending" />
                <Text style={type.small}>{formatDuration(item.duration_seconds)}</Text>
              </View>
              <Text style={[type.bodySoft, { marginTop: space.sm }]}>
                {item.attempts === 0
                  ? "Waiting to upload."
                  : `Retrying — ${item.last_error ?? "upload failed"} (attempt ${item.attempts}).`}
              </Text>
            </Card>
          ))}
        </View>
      }
      renderItem={({ item }) => <CaptureRow capture={item} onOpen={onOpen} />}
    />
  );
}

function CaptureRow({ capture, onOpen }: { capture: Capture; onOpen: (id: string) => void }) {
  const when = new Date(capture.captured_at);
  return (
    <Pressable
      onPress={() => onOpen(capture.id)}
      style={({ pressed }) => pressed && { opacity: 0.7 }}
      accessibilityRole="button"
      accessibilityHint="Opens the note to read or edit it"
    >
      <Card>
        <View style={styles.row}>
          <Text style={type.small}>
            {when.toLocaleDateString(undefined, { day: "numeric", month: "short" })}
            {"  "}
            {when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
          </Text>
          <View style={[styles.row, { gap: space.sm }]}>
            {capture.duration_seconds ? (
              <Text style={type.small}>{formatDuration(capture.duration_seconds)}</Text>
            ) : null}
            {capture.edited ? <Pill text="Edited" tone="done" /> : null}
            <Pill text={LABEL[capture.status]} tone={TONE[capture.status]} />
          </View>
        </View>

        <Text style={[type.body, { marginTop: space.md }]} numberOfLines={6}>
          {capture.text?.trim()
            ? capture.text
            : capture.status === "failed"
              ? "This one couldn't be transcribed."
              : "Transcribing…"}
        </Text>
      </Card>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
  queued: { borderStyle: "dashed", borderColor: colors.pending },
  banner: {
    backgroundColor: colors.pendingTint,
    borderRadius: radius.sm,
    padding: space.md,
  },
});
