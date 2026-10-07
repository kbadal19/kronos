# Second Mind — mobile

Open it, and the microphone is already live. Talk. That's the app.

Everything else — transcription, splitting a rambling session into
separate thoughts, extraction, linking — happens on the server. See
`../backend`.

## The one rule

**Once the recording stops, the thought must be safe.** Not "uploading",
not "probably fine" — written to SQLite on disk, pointing at a file on
disk, before any reassuring text appears on screen. The app can then be
killed, the battery can die, the network can be gone for three days, and
the recording still goes out. Nothing about delivery depends on a React
component staying mounted.

That's `src/outbox.ts`, and it's the most important file here.

## Running it

```bash
npm install
npx expo run:android          # a real device or emulator; Expo Go can't
                              # do expo-audio's native recording
```

The server is entered on the login screen and kept on the phone, so one
APK works against a laptop on the same Wi-Fi (`192.168.x.x:8000`, plain
http) or the real server (a hostname, https). `app.json` → `extra.apiBase`
is only the pre-filled default.

```bash
npm run typecheck             # tsc --noEmit
npx expo-doctor               # config + peer dependency validation
```

## Building the APK

```bash
npx expo prebuild --platform android --clean
cd android && ./gradlew assembleRelease   # JAVA_HOME = Android Studio's jbr (JDK 21)
# -> android/app/build/outputs/apk/release/app-release.apk
```

~15MB: arm64 only, R8 minify + resource shrinking, compressed native libs
(all set via expo-build-properties in app.json). A universal APK with all
four ABIs and no shrinking was 81MB. R8 can't see classes loaded by name —
`extraProguardRules` keeps the headless app loader the background outbox
drain needs; test a release build on a device after adding any native
module.

Sideloaded, so there's no store to push updates through — which is why
`expo-updates` is wired in. JS-only changes ship over the air; only a
native dependency change needs a new APK on the cable.

## Design decisions worth knowing

**It is dark, always.** Used in bed at 4am. A white screen wakes you up
properly, and an app that wakes you up is one you stop opening. Even the
recording red is desaturated below what a daytime UI would use.

**There are no custom fonts.** Loading a webfont blocks first render, and
first render is when the microphone opens. 400ms of mic latency is a bad
trade for nicer letterforms in an app whose whole job is catching a
thought before it's gone. Same reasoning behind importing
`@expo/vector-icons/Ionicons` directly rather than the barrel — that
alone is 3.5MB of icon fonts down to 390KB.

**The mic opens on launch**, with rails that make that tolerable:
anything under 1.2s is dropped silently (pocket opens, "just checking the
timeline" opens), Discard is always one tap away and never confirms, and
the whole behaviour can be switched off.

**Recording continues when you switch to the timeline.** Losing audio
because you glanced away would be worse. But it is never invisible — the
Capture tab carries a live dot, because a recording you can't see is one
you forget and come back to find ten minutes of a quiet room.

**Voice quality, not music quality.** 22kHz mono at 32kbps, ~180KB for a
45-second thought, against ~700KB for the stock HIGH_QUALITY preset — for
zero transcription benefit, since recognizers downmix to 16kHz mono
anyway. That's the difference between an upload that completes on one bar
of signal and one that doesn't.

**There's a level meter**, and it isn't decoration: it's the only way to
tell "recording" from "recording silence". A muted mic at 4am costs you
the thought with no way to find out until morning.

## Layout

```
App.tsx                  two tabs, no router; both screens stay mounted
src/api.ts               axios + JWT, deduped refresh interceptor
src/outbox.ts            SQLite queue, backoff, stranded-file sweep
src/recorder.ts          recording options + background drain task
src/theme.ts             dark tokens
src/screens/
  CaptureScreen.tsx      the point of the app
  TimelineScreen.tsx     time is the spine
  LoginScreen.tsx        once, then a year
```

## Gotchas

- **`useKeepAwake()` holds the lock for the component's entire lifetime**,
  and the tag argument does not gate it. Since CaptureScreen never
  unmounts, using the hook kept the display on forever. Use
  `activateKeepAwakeAsync`/`deactivateKeepAwake` in an effect instead.
- **RN 0.86 dropped `StyleSheet.absoluteFillObject`** from its types.
  `absoluteFill` is the current name.
- **`expo-font` and `expo-asset` are required peer dependencies** of
  `@expo/vector-icons` and `expo-audio`. Everything works in Expo Go
  without them and crashes in a real build — exactly the case here.
- **`setAudioModeAsync({ allowsRecording: true })` must run before the
  first `record()`** or Android routes the mic to the wrong source and
  captures silence.
