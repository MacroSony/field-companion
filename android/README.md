# Field Probe (Android)

Minimal native probe for the field-companion audio path: **USB-C wireless-mic receiver in, Bluetooth (A2DP) earbuds out**, without dropping into call mode. It records raw PCM on the phone and logs every routing change, so we can check on a real device whether this setup works before building the full app.

Status: v0.2.0, tested on an Android 16 (API 36) emulator only. Not yet tested on a real phone.

## What it does

- Lists every input and output device (type, name, supported rates/channels/encodings). **Save device report** writes it all to JSON.
- Records through a foreground microphone service, so it keeps going with the screen off. Format: 48 kHz, 16-bit, stereo or mono; you choose the input device and `AudioSource`. `UNPROCESSED` is the default; `VOICE_COMMUNICATION` is there on purpose to reproduce call mode for comparison.
- WAV files are split into 30-minute segments. The header is rewritten every ~10 s, so a crash or a dead battery still leaves a readable file.
- Logs these events with timestamps and sample frames:
  - audio mode changes (`NORMAL` vs `IN_COMMUNICATION`)
  - communication-device and input-route changes
  - devices plugged in or removed
  - whether the system silenced our recording (`isClientSilenced`)
- Shows live L/R RMS and peak levels plus the L−R difference, to check that the two transmitters really land on separate channels.
- Plays cue sounds as `USAGE_MEDIA` to a chosen output (Chime, or a 3 s 500 Hz→8 kHz sweep) and logs the device each cue actually played on.
- **Marks**: from the button, the notification action, or headset media keys (play/pause, next, …). Each mark plays a short chime.
- When recording stops, everything is copied to `Download/FieldProbe/`: `probe-<stamp>_NN.wav` and `probe-<stamp>.json`.
- **Link (optional, v0.2)**: the app becomes the backend's native mic client (`/ws?role=mic`, websocket transport) while it keeps recording locally.
  - Uplink: one channel (L by default, the collar mic) as mono 48 kHz PCM16, sent in 20 ms frames.
  - Downlink: the assistant's 24 kHz speech is played as media, with `playback {active, response_id, drained}` reports matching web/app.js.
  - Also handles server `flush`/`earcon`, has a *Tap* control (button and notification), reconnects automatically, and logs transcripts and server events to the session JSON.
  - *Open web UI* opens the existing page in a WebView as a `display` client of the same session.
- **Fake mic**: point the app at a 48 kHz PCM16 WAV and it streams that instead of the microphone, in real time. Used for repeatable emulator tests.

## Build

```bash
. /data/opt/android-env.sh       # local JDK 21 + SDK + Gradle cache under /data/opt (not in repo)
cd android && ./gradlew assembleDebug
# -> app/build/outputs/apk/debug/app-debug.apk
```

Toolchain: AGP 9.4.1 with built-in Kotlin, Gradle 9.6.0, compileSdk/targetSdk 36, minSdk 33. No AndroidX or other third-party dependencies.

## First real-device test (DJI Mic Mini 2 + mobile receiver, OPPO Enco Free 4)

Before you start: set the mobile receiver's noise-cancel switch to **O**. In DJI Mimo, set the channel mode to **stereo**. On MagicOS, set the app's battery setting to *manage manually / allow background activity*, otherwise the foreground service may be killed.

1. **Device report.** Plug in the receiver and connect the Enco, then tap *Save device report*. Expect a `usb_device`/`usb_headset` input that lists 2 channels and 48000, and a `bluetooth_a2dp` output.
2. **Stereo split.** Input = USB, source = UNPROCESSED, Stereo. Record, then talk into TX1 and TX2 in turn. Expect **L−R** to swing strongly positive, then negative.
3. **No call mode.** With the Enco connected, record and play *Chime*/*Sweep* to the A2DP output. Expect `mode=NORMAL`, `comm` not Bluetooth, the cue routed to `bluetooth_a2dp`, and no `audio_mode` event in the JSON. For comparison, repeat with `VOICE_COMMUNICATION` and see what changes.
4. **High frequencies survive.** Record the sweep or real birdsong with NC=O, then compare a spectrogram against the phone's built-in mic (UNPROCESSED).
5. **Screen off / pocket.** Record, lock the screen, walk a loop with TX2 on your back and the phone in a front pocket, mark dropouts with the earbud tap, then unlock and stop. Check that the duration matches wall time, `client_silenced` is never true, and there are no gaps.
6. **Power.** Note the battery % per hour with and without a power bank through the receiver's USB-C pass-through.

Pull files with any file manager (`Download/FieldProbe`), or `adb pull /sdcard/Download/FieldProbe`.

## Emulator end-to-end test

```bash
. /data/opt/android-env.sh && ./gradlew assembleDebug
emulator -avd probe36 -no-window -gpu swiftshader_indirect -port 5584 &   # AVD under /data/opt/avd
ADB_SERIAL=emulator-5584 tools/emulator-link-test.sh <web-dir-containing-mock-server.py> <python-with-websockets>
```

The script starts `web/mock-server.py`, installs the APK, presets the link URL and fake WAV, and drives the UI: record → Tap → open web UI → stop. It then checks the mock log for the mic hello, 20 ms uplink frames, the tap, playback active/drained, and the display client joining the same session. Last run (2026-10-26, mock from `feat/gpt-realtime`): 6/6 PASS, 0 dropped frames.

## Known limits

- Tested on the emulator only: no USB audio, no Bluetooth.
- Depending on the earbud's gesture settings (HeyMelody app), a tap may not send media keys. Media keys reach us only while our `MediaSession` is the active one; the start chime is there to help with that.
- In the WebView, display mode currently renders the laptop "big screen" layout, which is too cramped on a phone. A phone-shell mode in web/app.js is needed (see DESIGN.md).
- PCM is 16-bit. If the USB receiver reports a 24-bit native format, Android converts it; the `actual` block in the JSON records what we got.
