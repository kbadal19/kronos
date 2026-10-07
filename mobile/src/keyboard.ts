/**
 * How far to lift content so the keyboard doesn't cover it — Android only.
 *
 * The app draws edge to edge, and in that mode Android no longer resizes
 * the window for the keyboard; KeyboardAvoidingView misjudged the offset
 * with a tab bar underneath. So the shell measures the keyboard's top edge
 * and lifts its content to exactly there. Measured on a Pixel emulator:
 * screen 923dp, keyboard top at 587dp, reported height 312dp — the height
 * excludes the 24dp navigation strip, which is why the top edge (screenY)
 * is used rather than the height. iOS keeps per-screen
 * KeyboardAvoidingView, which handles it correctly.
 */
import { useEffect, useState } from "react";
import { Dimensions, Keyboard, Platform } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

export function useKeyboardLift(): number {
  const [top, setTop] = useState<number | null>(null);
  const insets = useSafeAreaInsets();

  useEffect(() => {
    if (Platform.OS !== "android") return;
    const show = Keyboard.addListener("keyboardDidShow", (e) => setTop(e.endCoordinates.screenY));
    const hide = Keyboard.addListener("keyboardDidHide", () => setTop(null));
    return () => {
      show.remove();
      hide.remove();
    };
  }, []);

  if (top === null) return 0;
  // Content ends at the safe-area bottom (the tab bar hides while typing),
  // so lift by the distance from there up to the keyboard's top edge.
  const contentBottom = Dimensions.get("screen").height - insets.bottom;
  return Math.max(0, contentBottom - top);
}
