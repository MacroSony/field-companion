#!/usr/bin/env bash
# End-to-end smoke test of the native mic client against web/mock-server.py on an emulator.
# Usage: tools/emulator-link-test.sh <path-to-web-dir-with-mock-server.py> [python]
# Needs: a booted emulator (adb), the debug APK built, Python with `websockets`.
set -euo pipefail
WEB=${1:?web dir}; PY=${2:-python3}
P=com.macrosony.fieldprobe; A="adb ${ADB_SERIAL:+-s $ADB_SERIAL}"
HERE=$(cd "$(dirname "$0")/.." && pwd); LOG=$(mktemp); WAV=$(mktemp --suffix=.wav)

"$PY" - "$WAV" <<'PY'
import math, struct, sys, wave
r = 48000
with wave.open(sys.argv[1], 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(r)
    w.writeframes(b''.join(struct.pack('<h', int(6000 * math.sin(2 * math.pi * 300 * i / r) * (int(i / r * 2) % 2 == 0)))
                           for i in range(r * 10)))
PY
(cd "$WEB" && exec "$PY" mock-server.py --port 8767 --host 127.0.0.1 --offline >"$LOG" 2>&1) &
MOCK=$!; trap 'kill $MOCK 2>/dev/null || true' EXIT; sleep 2

$A install -r "$HERE/app/build/outputs/apk/debug/app-debug.apk" >/dev/null
for p in RECORD_AUDIO POST_NOTIFICATIONS BLUETOOTH_CONNECT CAMERA; do $A shell pm grant $P android.permission.$p; done
$A shell am force-stop $P
$A push "$WAV" /data/local/tmp/fake.wav >/dev/null
$A shell run-as $P mkdir -p files shared_prefs
$A shell run-as $P cp /data/local/tmp/fake.wav files/fake.wav
PREFS=$(mktemp); cat >"$PREFS" <<XML
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="link_url">ws://10.0.2.2:8767/ws?session=android-test&amp;role=mic</string>
    <int name="link_channel" value="0" />
    <string name="fake_wav">/data/user/0/$P/files/fake.wav</string>
</map>
XML
$A push "$PREFS" /data/local/tmp/probe.xml >/dev/null
$A shell run-as $P cp /data/local/tmp/probe.xml shared_prefs/probe.xml
$A shell am start -n $P/.MainActivity >/dev/null; sleep 3

$A shell uiautomator dump /sdcard/ui.xml >/dev/null; $A pull /sdcard/ui.xml /tmp/fp-ui.xml >/dev/null
xy() { "$PY" -c "import re,sys;s=open('/tmp/fp-ui.xml').read();m=re.search(r'text=\"'+re.escape(sys.argv[1])+r'\"[^>]*bounds=\"\[(\d+),(\d+)\]\[(\d+),(\d+)\]\"',s);a,b,c,d=map(int,m.groups());print((a+c)//2,(b+d)//2)" "$1"; }
REC=$(xy "● Record"); STOP=$(xy "■ Stop"); TAP=$(xy "Tap"); WEBUI=$(xy "Open web UI (display client)")

$A shell input tap $REC; sleep 5
$A shell input tap $TAP; sleep 3
$A shell input tap $WEBUI; sleep 6
$A shell input keyevent KEYCODE_BACK; sleep 2
$A shell input tap $STOP; sleep 3

fail=0
check() { if grep -q "$1" "$LOG"; then echo "PASS  $2"; else echo "FAIL  $2"; fail=1; fi; }
check 'hello role=mic session=android-test' 'mic client hello'
check 'mic PCM frame #50: 1920 bytes' 'uplink 20 ms mono 48 kHz frames'
check '"type":"tap"' 'tap sent'
check '"active":true' 'playback reported active'
check '"drained":true' 'playback reported drained'
check 'hello role=display session=android-test' 'web UI joined same session as display'
grep -o 'closed role=mic; received [0-9]* microphone frames' "$LOG" || true
exit $fail
