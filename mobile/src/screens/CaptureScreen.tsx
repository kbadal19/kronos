/**
 * The whole point of the app.
 *
 * Open it and the microphone is already live. No button to find, no
 * "new note" flow, no category to pick — at 4am every extra tap is a
 * chance for the thought to evaporate, and the pipeline does the
 * categorising anyway.
 *
 * The safety rails that make auto-recording tolerable:
 *   - anything under MIN_CAPTURE_SECONDS is dropped silently, so pocket
 *     opens and "just checking the timeline" opens leave no trace;
 *   - Discard is always one tap away and never asks for confirmation;
 *   - auto-start can be turned off, for the day it annoys you.
 *
 * On stop, the recording is written to the outbox *before* the UI says
 * anything reassuring. The user is told the thought is safe only once it
 * actually is.
 */
import {
  AudioModule,
  useAudioRecorder,
  useAudioRecorderState,
  setAudioModeAsync,
} from "expo-audio";
import * as Crypto from "expo-crypto";
import { activateKeepAwakeAsync, deactivateKeepAwake } from "expo-keep-awake";
import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Pressable, StyleSheet, Switch, Text, View } from "react-native";

import { drain, enqueue, pendingCount } from "../outbox";
import { MIN_CAPTURE_SECONDS, VOICE_RECORDING, formatDuration } from "../recorder";
import { micHeld } from "../voice";
import { colors, radius, space, type } from "../theme";
import { Pill } from "../ui";

const KEEP_AWAKE_TAG = "secondmind-recording";

/** What the rest of the app may do to a recording in progress: save it
 *  (the usual rules — too short is dropped) before Ask or Teach take the
 *  mic. Saving, never discarding: it may be a thought. */
export type CaptureControl = { save: () => Promise<void> };

type Props = {
  autoStart: boolean;
  onAutoStartChange: (next: boolean) => void;
  onSaved: () => void;
  /** Lifted so the tab bar can show a live dot. Recording deliberately
   *  continues while you look at the timeline — losing audio because you
   *  glanced away would be worse — but it must never be invisible, or
   *  you come back to a ten-minute recording of a quiet room. */
  onRecordingChange: (recording: boolean) => void;
  control?: React.MutableRefObject<CaptureControl | null>;
};

export default function CaptureScreen({
  autoStart,
  onAutoStartChange,
  onSaved,
  onRecordingChange,
  control,
}: Props) {
  const recorder = useAudioRecorder(VOICE_RECORDING);
  const state = useAudioRecorderState(recorder, 200);

  const [permission, setPermission] = useState<"unknown" | "granted" | "denied">("unknown");
  const [queued, setQueued] = useState(0);
  const [flash, setFlash] = useState<string | null>(null);
  // Guards the async gap in start(): without it, a fast
  // background/foreground bounce can fire two record() calls and the
  // second one silently discards the first file.
  const starting = useRef(false);

  // Only while recording. A screen that sleeps mid-thought is fine — the
  // recorder keeps running — but you then have to unlock the phone to
  // stop it, holding a thought you were trying to put down.
  //
  // Deliberately not useKeepAwake(): that hook holds the lock for the
  // component's whole lifetime, and this screen never unmounts, so it
  // would keep the display on forever.
  useEffect(() => {
    onRecordingChange(state.isRecording);
  }, [onRecordingChange, state.isRecording]);

  useEffect(() => {
    if (!state.isRecording) return;
    activateKeepAwakeAsync(KEEP_AWAKE_TAG).catch(() => {});
    return () => {
      deactivateKeepAwake(KEEP_AWAKE_TAG).catch(() => {});
    };
  }, [state.isRecording]);

  const refreshQueue = useCallback(() => {
    pendingCount().then(setQueued).catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const result = await AudioModule.requestRecordingPermissionsAsync();
      if (cancelled) return;
      setPermission(result.granted ? "granted" : "denied");
      if (result.granted) {
        // allowsRecording has to be set before the first record() or
        // Android routes the mic to the wrong source and captures silence.
        await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true });
      }
    })();
    refreshQueue();
    return () => {
      cancelled = true;
    };
  }, [refreshQueue]);

  const start = useCallback(async () => {
    // micHeld: Ask or Teach is listening. Coming back to the foreground
    // mid-question must not open a second recorder over it.
    if (permission !== "granted" || state.isRecording || starting.current || micHeld()) return;
    starting.current = true;
    try {
      await recorder.prepareToRecordAsync(VOICE_RECORDING);
      recorder.record();
    } catch {
      setFlash("Could not open the mic");
    } finally {
      starting.current = false;
    }
  }, [permission, recorder, state.isRecording]);

  const finish = useCallback(
    async (keep: boolean) => {
      if (!state.isRecording) return;
      const seconds = state.durationMillis / 1000;

      await recorder.stop();
      const uri = recorder.uri;

      if (!keep || !uri || seconds < MIN_CAPTURE_SECONDS) {
        // Too short to be a thought. Say nothing — announcing a discard
        // every time you open the app to read the timeline is noise.
        if (keep && uri && seconds < MIN_CAPTURE_SECONDS) setFlash("Too short — discarded");
        return;
      }

      await enqueue({
        id: Crypto.randomUUID(),
        uri,
        captured_at: new Date().toISOString(),
        duration_seconds: Number(seconds.toFixed(2)),
      });

      // Only now is it true.
      setFlash(`Saved ${formatDuration(seconds)}`);
      refreshQueue();
      onSaved();

      // Fire and forget: the outbox owns delivery from here, so a failed
      // upload is not this screen's problem and must not block it.
      drain().then(refreshQueue).catch(() => {});
    },
    [onSaved, recorder, refreshQueue, state.durationMillis, state.isRecording]
  );

  if (control) control.current = { save: () => finish(true) };

  // Auto-start on mount and whenever the app comes back to the foreground —
  // and only then. `start` is recreated every time recording stops, so it
  // must not be a dependency here: that re-ran this effect on every Done or
  // Discard and silently reopened the mic on an empty room.
  const startRef = useRef(start);
  startRef.current = start;

  useEffect(() => {
    if (!autoStart || permission !== "granted") return;
    startRef.current();

    const sub = AppState.addEventListener("change", (next) => {
      if (next === "active") startRef.current();
    });
    return () => sub.remove();
  }, [autoStart, permission]);

  // Stopping on background would truncate a thought you're still speaking
  // while glancing at a notification, so recording deliberately continues.
  // What we do flush is the queue.
  useEffect(() => {
    const sub = AppState.addEventListener("change", (next) => {
      if (next === "background") drain().catch(() => {});
    });
    return () => sub.remove();
  }, []);

  useEffect(() => {
    if (!flash) return;
    const t = setTimeout(() => setFlash(null), 2600);
    return () => clearTimeout(t);
  }, [flash]);

  if (permission === "denied") {
    return (
      <View style={styles.center}>
        <Text style={type.title}>The mic is off</Text>
        <Text style={[type.bodySoft, { textAlign: "center", marginTop: space.sm }]}>
          SANS cannot do anything without it. Enable microphone access in
          Settings and reopen the app.
        </Text>
      </View>
    );
  }

  const seconds = state.durationMillis / 1000;
  const live = state.isRecording;

  return (
    <View style={styles.root}>
      <View style={styles.statusRow}>
        {live ? <Pill text="Listening" tone="live" /> : <Pill text="Idle" />}
        {queued > 0 ? <Pill text={`${queued} queued`} tone="pending" /> : null}
      </View>

      <View style={styles.stage}>
        <Text style={type.timer}>{formatDuration(seconds)}</Text>
        <Text style={[type.bodySoft, { marginTop: space.sm, textAlign: "center" }]}>
          {live
            ? "Talk about anything. Jump between topics — it gets split up later."
            : "Tap to start talking."}
        </Text>
        <Meter metering={state.metering} live={live} />
      </View>

      <View style={styles.controls}>
        {live ? (
          <>
            <Pressable
              onPress={() => finish(false)}
              style={({ pressed }) => [styles.ghost, pressed && styles.pressed]}
              accessibilityLabel="Discard this recording"
            >
              <Text style={[type.body, { color: colors.textSoft }]}>Discard</Text>
            </Pressable>
            <Pressable
              onPress={() => finish(true)}
              style={({ pressed }) => [styles.primary, pressed && styles.pressed]}
              accessibilityLabel="Stop and save"
            >
              <Text style={styles.primaryText}>Done</Text>
            </Pressable>
          </>
        ) : (
          <Pressable
            onPress={start}
            style={({ pressed }) => [styles.primary, styles.primaryWide, pressed && styles.pressed]}
            accessibilityLabel="Start recording"
          >
            <Text style={styles.primaryText}>Start talking</Text>
          </Pressable>
        )}
      </View>

      <View style={styles.footer}>
        <Text style={type.small}>{flash ?? " "}</Text>
        <View style={styles.autoRow}>
          <Text style={type.small}>Mic opens on launch</Text>
          <Switch
            value={autoStart}
            onValueChange={onAutoStartChange}
            trackColor={{ true: colors.accent, false: colors.rule }}
            thumbColor={colors.text}
          />
        </View>
      </View>
    </View>
  );
}

/** A coarse level meter.
 *
 *  Not decoration: it is the only way to tell "recording" from
 *  "recording silence", and a muted mic at 4am costs you the thought
 *  entirely with no way to find out until morning. */
function Meter({ metering, live }: { metering?: number; live: boolean }) {
  const bars = 28;
  // Metering is dBFS, roughly -60 (silence) to 0 (clipping).
  const level = live && typeof metering === "number" ? Math.max(0, (metering + 60) / 60) : 0;
  const lit = Math.round(level * bars);

  return (
    <View style={styles.meter}>
      {Array.from({ length: bars }).map((_, i) => (
        <View
          key={i}
          style={{
            flex: 1,
            height: 3 + (i < lit ? 15 * (1 - Math.abs(i - bars / 2) / bars) : 0),
            borderRadius: 2,
            backgroundColor: i < lit ? colors.live : colors.rule,
          }}
        />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, padding: space.xl, justifyContent: "space-between" },
  center: { flex: 1, alignItems: "center", justifyContent: "center", padding: space.xxl },
  statusRow: { flexDirection: "row", gap: space.sm },
  stage: { flex: 1, alignItems: "center", justifyContent: "center", gap: space.sm },
  meter: {
    flexDirection: "row",
    alignItems: "center",
    gap: 3,
    height: 20,
    marginTop: space.xl,
    alignSelf: "stretch",
    paddingHorizontal: space.xl,
  },
  controls: { flexDirection: "row", gap: space.md, alignItems: "center" },
  primary: {
    flex: 1,
    backgroundColor: colors.accent,
    borderRadius: radius.lg,
    paddingVertical: space.lg + 2,
    alignItems: "center",
  },
  primaryWide: { flex: 1 },
  primaryText: { ...type.title, color: colors.night },
  ghost: {
    paddingVertical: space.lg + 2,
    paddingHorizontal: space.xl,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.rule,
  },
  pressed: { opacity: 0.7 },
  footer: { marginTop: space.xl, gap: space.md },
  autoRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
});
