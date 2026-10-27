# Field Probe (Android)

Minimal native probe for the field-companion audio path: **USB-C wireless-mic receiver in, Bluetooth (A2DP) earbuds out**, without dropping into call mode. It records raw PCM on the phone and logs every routing change, so we can check on a real device whether this setup works before building the full app.

Status: v0.3.0. Real-device indoor tests passed on 2026-10-27 (Honor LDY-AN00, Android 16, DJI Mic Mini 2 mobile RX + OPPO Enco Free 4); see the results section below.

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

Toolchain: AGP 9.4.1 with built-in Kotlin, Gradle 9.6.0, compileSdk/targetSdk 36, minSdk 33. One dependency: OkHttp 4.12 (no AndroidX).

## First real-device test (DJI Mic Mini 2 + mobile receiver, OPPO Enco Free 4)

Before you start: set the mobile receiver's noise-cancel switch to **O**. In DJI Mimo, set the channel mode to **stereo**. On MagicOS, set the app's battery setting to *manage manually / allow background activity*, otherwise the foreground service may be killed.

1. **Device report.** Plug in the receiver and connect the Enco, then tap *Save device report*. Expect a `usb_device`/`usb_headset` input that lists 2 channels and 48000, and a `bluetooth_a2dp` output.
2. **Stereo split.** Input = USB, source = UNPROCESSED, Stereo. Record, then talk into TX1 and TX2 in turn. Expect **L−R** to swing strongly positive, then negative.
3. **No call mode.** With the Enco connected, record and play *Chime*/*Sweep* to the A2DP output. Expect `mode=NORMAL`, `comm` not Bluetooth, the cue routed to `bluetooth_a2dp`, and no `audio_mode` event in the JSON. For comparison, repeat with `VOICE_COMMUNICATION` and see what changes.
4. **High frequencies survive.** Record the sweep or real birdsong with NC=O, then compare a spectrogram against the phone's built-in mic (UNPROCESSED).
5. **Screen off / pocket.** Record, lock the screen, walk a loop with TX2 on your back and the phone in a front pocket, mark dropouts with the earbud tap, then unlock and stop. Check that the duration matches wall time, `client_silenced` is never true, and there are no gaps.
6. **Power.** Note the battery % per hour with and without a power bank through the receiver's USB-C pass-through.

Pull files with any file manager (`Download/FieldProbe`), or `adb pull /sdcard/Download/FieldProbe`.

## Real-device results (2026-10-27, indoor)

- USB input `Wireless Mic Rx`: 48 kHz, 1/2 ch, pcm16/float/pcm24. **DJI Mimo must be set to stereo**; the default sends identical L/R.
- Stereo: corr(L,R) = 0.0035; talking into one TX gives 17–21 dB separation (acoustic bleed only). Quiet floor about -61 dBFS RMS. Speech is still ~25 dB above the floor at 16–20 kHz; nothing above 20 kHz.
- `mode=NORMAL` throughout, never silenced, every cue routed to `bluetooth_a2dp`. An Enco gesture arrives as `KEYCODE_MEDIA_PLAY`.
- Link mode over a cloudflared tunnel against `feat/gpt-realtime` (websocket transport, Ambient): conversation works with `--realtime-provider openai`; BirdNET heard played Killdeer / Great Horned Owl calls from the left channel.
- Gotchas: Mimo firmware updates hang while we hold the USB stream. MagicOS asks for on-phone confirmation for every `adb install`. `higgs-realtime-1.5-preview` was retired; plain `higgs-realtime` connects, but the backend's route step timed out (not yet investigated).
- v0.3: in link mode an earbud gesture sends **Tap** (also logged). The notification shows the latest transcript line, manual cues are logged, and *Open web UI* adds `shell=android` (needs the matching web/app.js change: phone layout as display + per-species dedupe).

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
