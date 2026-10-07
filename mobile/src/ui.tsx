import { ActivityIndicator, Text, View, type ViewStyle } from "react-native";

import { colors, radius, space, type } from "./theme";

export function Loading({ label }: { label?: string }) {
  return (
    <View style={{ flex: 1, alignItems: "center", justifyContent: "center", gap: space.md }}>
      <ActivityIndicator color={colors.textSoft} />
      {label ? <Text style={type.small}>{label}</Text> : null}
    </View>
  );
}

export function Pill({
  text,
  tone = "neutral",
}: {
  text: string;
  tone?: "neutral" | "live" | "pending" | "done" | "fail";
}) {
  const palette: Record<string, { bg: string; fg: string }> = {
    neutral: { bg: colors.surfaceHigh, fg: colors.textSoft },
    live: { bg: colors.liveTint, fg: colors.live },
    pending: { bg: colors.pendingTint, fg: colors.pending },
    done: { bg: colors.doneTint, fg: colors.done },
    fail: { bg: colors.liveTint, fg: colors.fail },
  };
  const { bg, fg } = palette[tone];
  return (
    <View
      style={{
        backgroundColor: bg,
        borderRadius: radius.pill,
        paddingHorizontal: space.md,
        paddingVertical: 5,
        alignSelf: "flex-start",
      }}
    >
      <Text style={[type.label, { color: fg }]}>{text.toUpperCase()}</Text>
    </View>
  );
}

export function Card({ children, style }: { children: React.ReactNode; style?: ViewStyle }) {
  return (
    <View
      style={[
        {
          backgroundColor: colors.surface,
          borderRadius: radius.md,
          borderWidth: 1,
          borderColor: colors.rule,
          padding: space.lg,
        },
        style,
      ]}
    >
      {children}
    </View>
  );
}

export function Empty({ title, body }: { title: string; body: string }) {
  return (
    <View
      style={{
        flex: 1,
        alignItems: "center",
        justifyContent: "center",
        padding: space.xxl,
        gap: space.sm,
      }}
    >
      <Text style={[type.title, { textAlign: "center" }]}>{title}</Text>
      <Text style={[type.bodySoft, { textAlign: "center" }]}>{body}</Text>
    </View>
  );
}
