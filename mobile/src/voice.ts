/**
 * Short clips for talking *to* Krono — a question on Ask, a fact to
 * remember on Knowledge — as opposed to captures, which talk to yourself.
 *
 * A clip never goes through the outbox. Someone is looking at the screen
 * waiting for the answer, so it uploads straight away, and if that fails
 * you say it again: losing a question costs a few seconds, not a thought.
 * The file is deleted as soon as the request ends, either way.
 *
 * One microphone: while a clip records, Capture's auto-start stands down
 * (`micHeld`), and a capture still running when you tap a clip mic is
 * saved first — through the outbox, as any capture is — never discarded.
 */
import { AudioModule, setAudioModeAsync, useAudioRecorder, useAudioRecorderState } from "expo-audio";
import { File } from "expo-file-system";
import { useCallback, useEffect, useRef, useState } from "react";

import { VOICE_RECORDING } from "./recorder";

/** Shorter than this is a slipped tap, not a question. */
export const MIN_CLIP_SECONDS = 0.7;
/** A question is seconds. Stops on its own past this, so a forgotten mic
 *  doesn't upload ten minutes of a quiet room. */
export const MAX_CLIP_SECONDS = 120;

let held = false;
/** True while a clip is recording: Capture must not open the mic. */
export function micHeld(): boolean {
  return held;
}

export type Clip = { uri: string; seconds: number };

export function removeClip(uri: string) {
  try {
    const file = new File(uri);
    if (file.exists) file.delete();
  } catch {
    // The cache sweep on launch catches anything left behind.
  }
}

export function useClipRecorder() {
  const recorder = useAudioRecorder(VOICE_RECORDING);
  const state = useAudioRecorderState(recorder, 150);
  // Refs, not recorder state: state is polled, and a quick tap-tap would
  // read it before it caught up and leave the mic open.
  const active = useRef(false);
  const startedAt = useRef(0);
  const starting = useRef(false);
  const [recording, setRecording] = useState(false);

  const start = useCallback(async () => {
    if (active.current || starting.current) return;
    starting.current = true;
    try {
      const permission = await AudioModule.requestRecordingPermissionsAsync();
      if (!permission.granted) throw new Error("Microphone access is off — enable it in Settings");
      await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true });
      await recorder.prepareToRecordAsync(VOICE_RECORDING);
      recorder.record();
      active.current = held = true;
      startedAt.current = Date.now();
      setRecording(true);
    } finally {
      starting.current = false;
    }
  }, [recorder]);

  /** The clip, or null when it was too short (and already deleted). */
  const stop = useCallback(async (): Promise<Clip | null> => {
    if (!active.current) return null;
    const seconds = (Date.now() - startedAt.current) / 1000;
    active.current = held = false;
    setRecording(false);
    await recorder.stop();
    const uri = recorder.uri;
    if (!uri) return null;
    if (seconds < MIN_CLIP_SECONDS) {
      removeClip(uri);
      return null;
    }
    return { uri, seconds };
  }, [recorder]);

  const cancel = useCallback(async () => {
    const clip = await stop();
    if (clip) removeClip(clip.uri);
  }, [stop]);

  // Releasing the mic if the screen goes away mid-clip.
  useEffect(
    () => () => {
      if (active.current) held = false;
    },
    []
  );

  const seconds = recording ? state.durationMillis / 1000 : 0;
  return { start, stop, cancel, recording, seconds, metering: state.metering };
}
