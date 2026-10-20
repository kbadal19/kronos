/**
 * The mic for talking to SANS: one big target when idle, and while
 * listening a timer, a level meter (the only way to see a muted mic) and
 * Done / Cancel. Tap to start, tap to finish — not hold-to-talk, which is
 * hard to keep up lying in bed.
 */
import Ionicons from "@expo/vector-icons/Ionicons";
import { useEffect } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { formatDuration } from "./recorder";
import { colors, radius, space, type } from "./theme";
import { MAX_CLIP_SECONDS } from "./voice";

type Props = {
  recording: boolean;
  seconds: number;
  metering?: number;
  /** Waiting on the server: the mic is shown but can't be tapped. */
  busy: string | null;
  idleLabel: string;
  listeningHint: string;
  onStart: () => void;
  onDone: () => void;
  onCancel: () => void;
};

export function ClipControls({
  recording,
  seconds,
  metering,
  busy,
  idleLabel,
  listeningHint,
  onStart,
  onDone,
  onCancel,
}: Props) {
  useEffect(() => {
    if (recording && seconds >= MAX_CLIP_SECONDS) onDone();
  }, [recording, seconds, onDone]);

  if (recording) {
    return (
      <View style={styles.box}>
        <View style={styles.listeningRow}>
          <View style={styles.liveDot} />
          <Text style={[type.body, { fontVariant: ["tabular-nums"] }]}>{formatDuration(seconds)}</Text>
          <Text style={[type.small, { flex: 1 }]} numberOfLines={1}>
            {listeningHint}
          </Text>
        </View>
        <Meter metering={metering} />
        <View style={styles.actions}>
          <Pressable
            onPress={onCancel}
            style={({ pressed }) => [styles.ghost, pressed && styles.pressed]}
            accessibilityRole="button"
            accessibilityLabel="Cancel"
          >
            <Text style={[type.body, { color: colors.textSoft }]}>Cancel</Text>
          </Pressable>
          <Pressable
            onPress={onDone}
            style={({ pressed }) => [styles.done, pressed && styles.pressed]}
            accessibilityRole="button"
            accessibilityLabel="Done speaking"
          >
            <Ionicons name="arrow-up" size={20} color={colors.night} />
            <Text style={styles.doneText}>Done</Text>
          </Pressable>
        </View>
      </View>
    );
  }

  return (
    <View style={styles.box}>
      <Pressable
        onPress={onStart}
        disabled={busy !== null}
        style={({ pressed }) => [styles.mic, busy !== null && { opacity: 0.5 }, pressed && styles.pressed]}
        accessibilityRole="button"
        accessibilityLabel={idleLabel}
      >
        {busy !== null ? (
          <ActivityIndicator color={colors.night} />
        ) : (
          <Ionicons name="mic" size={22} color={colors.night} />
        )}
        <Text style={styles.doneText}>{busy ?? idleLabel}</Text>
      </Pressable>
    </View>
  );
}

function Meter({ metering }: { metering?: number }) {
  const bars = 24;
  // dBFS, roughly -60 (silence) to 0.
  const level = typeof metering === "number" ? Math.max(0, (metering + 60) / 60) : 0;
  const lit = Math.round(level * bars);
  return (
    <View style={styles.meter}>
      {Array.from({ length: bars }).map((_, i) => (
        <View
          key={i}
          style={{
            flex: 1,
            height: 3 + (i < lit ? 12 * (1 - Math.abs(i - bars / 2) / bars) : 0),
            borderRadius: 2,
            backgroundColor: i < lit ? colors.live : colors.rule,
          }}
        />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  box: { padding: space.md, borderTopWidth: 1, borderTopColor: colors.rule, gap: space.md },
  mic: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: space.sm,
    backgroundColor: colors.accent,
    borderRadius: radius.lg,
    paddingVertical: space.lg,
  },
  listeningRow: { flexDirection: "row", alignItems: "center", gap: space.sm, paddingHorizontal: space.xs },
  liveDot: { width: 10, height: 10, borderRadius: 5, backgroundColor: colors.live },
  meter: { flexDirection: "row", alignItems: "center", gap: 3, height: 16, paddingHorizontal: space.xs },
  actions: { flexDirection: "row", gap: space.md },
  ghost: {
    paddingVertical: space.lg,
    paddingHorizontal: space.xl,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.rule,
  },
  done: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: space.sm,
    backgroundColor: colors.accent,
    borderRadius: radius.lg,
    paddingVertical: space.lg,
  },
  doneText: { ...type.title, fontSize: 16, color: colors.night },
  pressed: { opacity: 0.7 },
});
