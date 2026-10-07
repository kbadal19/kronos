/**
 * Recording configuration and the background drain task.
 */
import * as BackgroundTask from "expo-background-task";
import { Paths } from "expo-file-system";
import * as TaskManager from "expo-task-manager";
import type { RecordingOptions } from "expo-audio";
import { AudioQuality, IOSOutputFormat } from "expo-audio";

import { drain } from "./outbox";

/**
 * Voice-note quality, not music quality.
 *
 * RecordingPresets.HIGH_QUALITY is 44.1kHz stereo at 128kbps — about
 * 700KB for a 45-second thought, for zero transcription benefit: speech
 * recognizers downmix to 16kHz mono internally anyway. Mono at 32kbps is
 * ~180KB for the same 45 seconds, which is the difference between an
 * upload that completes on one bar of signal and one that doesn't.
 *
 * 22.05kHz keeps comfortable headroom over the 8kHz of speech content
 * that actually matters.
 */
export const VOICE_RECORDING: RecordingOptions = {
  extension: ".m4a",
  sampleRate: 22050,
  numberOfChannels: 1,
  bitRate: 32000,
  // Needed for the level meter on the capture screen — without it the
  // UI can't tell "recording" from "recording silence", which is the
  // failure you most want to notice at 4am.
  isMeteringEnabled: true,
  android: {
    outputFormat: "mpeg4",
    audioEncoder: "aac",
  },
  ios: {
    outputFormat: IOSOutputFormat.MPEG4AAC,
    audioQuality: AudioQuality.MEDIUM, // Plenty for speech.
    linearPCMBitDepth: 16,
    linearPCMIsBigEndian: false,
    linearPCMIsFloat: false,
  },
  web: {
    mimeType: "audio/webm",
    bitsPerSecond: 32000,
  },
};

/** Recordings shorter than this are discarded without asking.
 *
 *  The mic opens automatically on launch, so pocket-opens and
 *  "check the timeline" opens would otherwise fill the graph with
 *  half-second nothings. */
export const MIN_CAPTURE_SECONDS = 1.2;

export const RECORDING_DIRECTORY = Paths.cache.uri;

const DRAIN_TASK = "secondmind-outbox-drain";

TaskManager.defineTask(DRAIN_TASK, async () => {
  try {
    const { sent } = await drain();
    return sent > 0
      ? BackgroundTask.BackgroundTaskResult.Success
      : BackgroundTask.BackgroundTaskResult.Success;
  } catch {
    return BackgroundTask.BackgroundTaskResult.Failed;
  }
});

/** Registers the periodic drain.
 *
 *  Android decides when this actually runs — the interval is a hint, and
 *  Doze can defer it for hours. That's acceptable precisely because it's
 *  a backstop: the foreground drain on app open is what normally ships
 *  a recording, and this only matters for the case where you speak and
 *  then don't open the app again for a day. */
export async function registerBackgroundDrain(): Promise<void> {
  try {
    const status = await BackgroundTask.getStatusAsync();
    if (status === BackgroundTask.BackgroundTaskStatus.Restricted) return;

    const already = await TaskManager.isTaskRegisteredAsync(DRAIN_TASK);
    if (already) return;

    await BackgroundTask.registerTaskAsync(DRAIN_TASK, { minimumInterval: 15 });
  } catch {
    // A phone that refuses background work still syncs on app open.
  }
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}
