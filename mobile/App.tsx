/**
 * Two screens and no router.
 *
 * Capture is home and it is where the app lands, always — the timeline is
 * something you go and look at, not something standing between you and
 * the microphone.
 */
import Ionicons from "@expo/vector-icons/Ionicons";
import * as SecureStore from "expo-secure-store";
import { StatusBar } from "expo-status-bar";
import * as Updates from "expo-updates";
import { useCallback, useEffect, useRef, useState } from "react";
import { BackHandler, Pressable, StyleSheet, Text, View } from "react-native";
import { SafeAreaProvider, SafeAreaView } from "react-native-safe-area-context";

import { hasStoredSession, logout, setOnSessionExpired } from "./src/api";
import { useKeyboardLift } from "./src/keyboard";
import { drain, initOutbox, sweepStrandedFiles } from "./src/outbox";
import { RECORDING_DIRECTORY, registerBackgroundDrain } from "./src/recorder";
import AskScreen from "./src/screens/AskScreen";
import CaptureScreen, { type CaptureControl } from "./src/screens/CaptureScreen";
import LoginScreen from "./src/screens/LoginScreen";
import NoteScreen from "./src/screens/NoteScreen";
import TimelineScreen from "./src/screens/TimelineScreen";
import EntityScreen from "./src/screens/EntityScreen";
import KnowledgeScreen from "./src/screens/KnowledgeScreen";
import { colors, space, type } from "./src/theme";
import { Loading } from "./src/ui";

type Tab = "capture" | "timeline" | "ask" | "knowledge";
type Overlay = { type: "note" | "entity"; id: string };
type AuthState = "loading" | "anon" | "authed";

const AUTOSTART_KEY = "sm_autostart";

export default function App() {
  return (
    <SafeAreaProvider>
      <AppShell />
    </SafeAreaProvider>
  );
}

function AppShell() {
  const [auth, setAuth] = useState<AuthState>("loading");
  const [tab, setTab] = useState<Tab>("capture");
  const [autoStart, setAutoStart] = useState(true);
  // Bumped when a capture is saved, so switching to the timeline shows it
  // without a manual pull-to-refresh.
  const [reloadKey, setReloadKey] = useState(0);
  const [recording, setRecording] = useState(false);
  // Pages opened over the current tab — a note from the timeline or an
  // answer, an entity from Knowledge, then a note from that entity. A stack,
  // not a router: Back pops one, and the tab underneath keeps its place.
  const [stack, setStack] = useState<Overlay[]>([]);
  const top = stack[stack.length - 1];
  const push = useCallback((o: Overlay) => setStack((s) => [...s, o]), []);
  const pop = useCallback(() => setStack((s) => s.slice(0, -1)), []);
  const openNote = useCallback((id: string) => push({ type: "note", id }), [push]);
  const openEntity = useCallback((id: string) => push({ type: "entity", id }), [push]);
  const keyboardLift = useKeyboardLift();
  // One mic. Asking or teaching while a capture runs saves the capture
  // first — the outbox takes it from there — and then takes the mic.
  const captureControl = useRef<CaptureControl | null>(null);
  const freeMic = useCallback(async () => {
    await captureControl.current?.save();
  }, []);

  useEffect(() => {
    if (!stack.length) return;
    const sub = BackHandler.addEventListener("hardwareBackPress", () => {
      pop();
      return true;
    });
    return () => sub.remove();
  }, [stack.length, pop]);

  const handleSessionExpired = useCallback(() => setAuth("anon"), []);

  useEffect(() => {
    setOnSessionExpired(handleSessionExpired);
  }, [handleSessionExpired]);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      // The outbox comes up before anything else. If a previous run was
      // killed with recordings still queued, they should be on their way
      // out before the user has finished reading the screen.
      await initOutbox();
      if (cancelled) return;

      const stored = await hasStoredSession();
      if (cancelled) return;
      setAuth(stored ? "authed" : "anon");

      const saved = await SecureStore.getItemAsync(AUTOSTART_KEY);
      if (!cancelled && saved !== null) setAutoStart(saved === "1");

      if (stored) {
        drain().catch(() => {});
        registerBackgroundDrain().catch(() => {});
      }
      sweepStrandedFiles(RECORDING_DIRECTORY).catch(() => {});
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  // Sideloaded APKs get no store updates, so OTA is the only way a fix
  // reaches the phone without a cable. Checked quietly in the background;
  // never blocks the mic.
  useEffect(() => {
    if (__DEV__) return;
    Updates.checkForUpdateAsync()
      .then((result) => (result.isAvailable ? Updates.fetchUpdateAsync() : null))
      .catch(() => {});
  }, []);

  const onAutoStartChange = useCallback((next: boolean) => {
    setAutoStart(next);
    SecureStore.setItemAsync(AUTOSTART_KEY, next ? "1" : "0").catch(() => {});
  }, []);

  if (auth === "loading") {
    return (
      <SafeAreaView style={styles.screen}>
        <StatusBar style="light" />
        <Loading />
      </SafeAreaView>
    );
  }

  if (auth === "anon") {
    return (
      <SafeAreaView style={styles.screen}>
        <StatusBar style="light" />
        <View style={{ flex: 1, paddingBottom: keyboardLift }}>
          <LoginScreen
            onDone={() => {
              setAuth("authed");
              drain().catch(() => {});
              registerBackgroundDrain().catch(() => {});
            }}
          />
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.screen}>
      <StatusBar style="light" />

      {/* Screens are absolutely positioned, and absolute children ignore
          their parent's padding — so the keyboard lift is a margin on this
          inner box, which they fill instead. */}
      <View style={[styles.body, { marginBottom: keyboardLift }]}>
        {/* Both screens stay mounted: unmounting Capture would tear down a
            recording in progress the moment you glanced at the timeline. */}
        <View style={[styles.page, tab !== "capture" && styles.hidden]} pointerEvents={tab === "capture" ? "auto" : "none"}>
          <CaptureScreen
            autoStart={autoStart}
            onAutoStartChange={onAutoStartChange}
            onSaved={() => setReloadKey((k) => k + 1)}
            onRecordingChange={setRecording}
            control={captureControl}
          />
        </View>
        <View style={[styles.page, tab !== "timeline" && styles.hidden]} pointerEvents={tab === "timeline" ? "auto" : "none"}>
          <TimelineScreen reloadKey={reloadKey} onOpen={openNote} />
        </View>
        {/* Kept mounted so the conversation survives a trip to another tab. */}
        <View style={[styles.page, tab !== "ask" && styles.hidden]} pointerEvents={tab === "ask" ? "auto" : "none"}>
          <AskScreen onOpenNote={openNote} freeMic={freeMic} />
        </View>
        {/* Kept mounted so a search survives opening an entity and coming back. */}
        <View style={[styles.page, tab !== "knowledge" && styles.hidden]} pointerEvents={tab === "knowledge" ? "auto" : "none"}>
          <KnowledgeScreen reloadKey={reloadKey} onOpenEntity={openEntity} freeMic={freeMic} />
        </View>
        {/* Over whichever tab opened it — a timeline row or an answer's
            citation — and back to that tab when closed. */}
        {top ? (
          <View style={styles.overlay}>
            {top.type === "note" ? (
              <NoteScreen key={`n${stack.length}`} id={top.id} onClose={pop} onChanged={() => setReloadKey((k) => k + 1)} />
            ) : (
              <EntityScreen
                key={`e${stack.length}`}
                id={top.id}
                onBack={pop}
                onOpenEntity={openEntity}
                onOpenNote={openNote}
                onChanged={() => setReloadKey((k) => k + 1)}
              />
            )}
          </View>
        ) : null}
      </View>

      {/* Hidden while typing: it would sit between the text box and the
          keyboard, and nobody switches tabs mid-sentence. */}
      <View style={[styles.tabs, keyboardLift > 0 && { display: "none" }]}>
        <TabButton
          icon="mic"
          label="Capture"
          active={tab === "capture"}
          dot={recording}
          onPress={() => {
            setStack([]);
            setTab("capture");
          }}
        />
        <TabButton
          icon="time-outline"
          label="Timeline"
          active={tab === "timeline"}
          onPress={() => {
            setReloadKey((k) => k + 1);
            setStack([]);
            setTab("timeline");
          }}
        />
        <TabButton
          icon="chatbubble-ellipses-outline"
          label="Ask"
          active={tab === "ask"}
          onPress={() => {
            setStack([]);
            setTab("ask");
          }}
        />
        <TabButton
          icon="library-outline"
          label="Knowledge"
          active={tab === "knowledge"}
          onPress={() => {
            setStack([]);
            setTab("knowledge");
          }}
        />
        <TabButton
          icon="log-out-outline"
          label="Sign out"
          active={false}
          onPress={async () => {
            await logout();
            setAuth("anon");
          }}
        />
      </View>
    </SafeAreaView>
  );
}

function TabButton({
  icon,
  label,
  active,
  dot,
  onPress,
}: {
  icon: keyof typeof Ionicons.glyphMap;
  label: string;
  active: boolean;
  dot?: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [styles.tab, pressed && { opacity: 0.6 }]}
      accessibilityRole="button"
      accessibilityLabel={label}
    >
      <View>
        <Ionicons name={icon} size={20} color={active ? colors.accent : colors.textFaint} />
        {dot ? <View style={styles.liveDot} /> : null}
      </View>
      <Text style={[type.label, active && { color: colors.accent }]}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.night },
  body: { flex: 1 },
  // absoluteFill, not absoluteFillObject — RN 0.86 dropped the latter
  // from its types.
  page: { ...StyleSheet.absoluteFill },
  hidden: { opacity: 0, zIndex: -1 },
  overlay: { ...StyleSheet.absoluteFill, backgroundColor: colors.night, zIndex: 1 },
  tabs: {
    flexDirection: "row",
    borderTopWidth: 1,
    borderTopColor: colors.rule,
    paddingTop: space.md,
    paddingBottom: space.sm,
  },
  tab: { flex: 1, alignItems: "center", gap: 3 },
  liveDot: {
    position: "absolute",
    top: -2,
    right: -4,
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.live,
  },
});
