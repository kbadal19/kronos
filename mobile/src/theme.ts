import type { FontVariant } from "react-native";

/** Visual tokens.
 *
 *  This app is used in bed at 4am, half asleep, to catch a thought before
 *  it evaporates. Two consequences drive every choice here:
 *
 *  1. It is dark. Always. Not a preference, not a toggle — a white screen
 *     at 4am wakes you up properly, and an app that wakes you up is one
 *     you stop opening. Even the "recording" red is muted below what a
 *     daytime UI would use.
 *
 *  2. There are no custom fonts. Loading a webfont blocks first render,
 *     and first render is when the microphone opens. Trading 400ms of
 *     mic latency for nicer letterforms is a bad trade in an app whose
 *     entire job is catching a thought before it's gone.
 */
export const colors = {
  // Near-black rather than pure black: pure #000 on OLED makes the edges
  // of cards disappear entirely and the layout stops being readable.
  night: "#0B0D10",
  surface: "#14171C",
  surfaceHigh: "#1C2027",
  rule: "#262B33",

  text: "#E8EAED",
  textSoft: "#9BA3AF",
  textFaint: "#6B7280",

  // The recording state. Desaturated on purpose — a saturated red at 4am
  // is a slap.
  live: "#D9564C",
  liveTint: "#2A1715",

  // Queued / in-flight work.
  pending: "#C99A3B",
  pendingTint: "#26200F",

  done: "#5A9E6F",
  doneTint: "#152218",

  fail: "#C4564A",

  accent: "#6E8FD8",
};

export const space = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 36 };

export const radius = { sm: 8, md: 14, lg: 22, pill: 999 };

export const type = {
  display: { fontSize: 40, lineHeight: 46, fontWeight: "300" as const, color: colors.text },
  title: { fontSize: 20, lineHeight: 26, fontWeight: "600" as const, color: colors.text },
  body: { fontSize: 15, lineHeight: 23, color: colors.text },
  bodySoft: { fontSize: 15, lineHeight: 23, color: colors.textSoft },
  small: { fontSize: 13, lineHeight: 18, color: colors.textSoft },
  label: {
    fontSize: 11,
    lineHeight: 14,
    letterSpacing: 1.1,
    fontWeight: "600" as const,
    color: colors.textFaint,
  },
  // Tabular figures so the recording timer doesn't jitter as digits change.
  timer: {
    fontSize: 52,
    lineHeight: 60,
    fontWeight: "200" as const,
    color: colors.text,
    // Not `as const`: TextStyle wants a mutable FontVariant[].
    fontVariant: ["tabular-nums"] as FontVariant[],
  },
};
