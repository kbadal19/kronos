import { useEffect, useState } from "react";
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { getErrorMessage, getServer, login, setServer } from "../api";
import { colors, radius, space, type } from "../theme";

export default function LoginScreen({ onDone }: { onDone: () => void }) {
  const [server, setServerInput] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getServer().then(setServerInput).catch(() => {});
  }, []);

  async function submit() {
    if (busy || !server || !username || !password) return;
    setBusy(true);
    setError(null);
    try {
      setServerInput(await setServer(server));
      await login(username.trim(), password);
      onDone();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <KeyboardAvoidingView
      style={styles.root}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <Text style={type.display}>SANS</Text>
      <Text style={[type.bodySoft, { marginTop: space.sm, marginBottom: space.xxl }]}>
        Sign in once. This phone stays signed in for a year.
      </Text>

      <TextInput
        value={server}
        onChangeText={setServerInput}
        placeholder="Server, e.g. 192.168.1.5:8000"
        placeholderTextColor={colors.textFaint}
        autoCapitalize="none"
        autoCorrect={false}
        keyboardType="url"
        // Not a credential. Without this, Android autofill treats the
        // first field as the username and saves the URL as your login.
        autoComplete="off"
        importantForAutofill="no"
        textContentType="URL"
        style={styles.input}
      />
      <TextInput
        value={username}
        onChangeText={setUsername}
        placeholder="Username"
        placeholderTextColor={colors.textFaint}
        autoCapitalize="none"
        autoCorrect={false}
        autoComplete="username"
        textContentType="username"
        style={styles.input}
      />
      <TextInput
        value={password}
        onChangeText={setPassword}
        placeholder="Password"
        placeholderTextColor={colors.textFaint}
        secureTextEntry
        autoCapitalize="none"
        autoComplete="current-password"
        textContentType="password"
        onSubmitEditing={submit}
        style={styles.input}
      />

      {error ? <Text style={[type.small, { color: colors.fail }]}>{error}</Text> : null}

      <Pressable
        onPress={submit}
        disabled={busy}
        style={({ pressed }) => [styles.button, (pressed || busy) && { opacity: 0.7 }]}
      >
        <Text style={styles.buttonText}>{busy ? "Signing in…" : "Sign in"}</Text>
      </Pressable>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, padding: space.xl, justifyContent: "center" },
  input: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.rule,
    borderRadius: radius.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.lg,
    marginBottom: space.md,
    color: colors.text,
    fontSize: 16,
  },
  button: {
    backgroundColor: colors.accent,
    borderRadius: radius.lg,
    paddingVertical: space.lg,
    alignItems: "center",
    marginTop: space.md,
  },
  buttonText: { ...type.title, color: colors.night },
});
