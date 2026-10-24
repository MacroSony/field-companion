"""Save matched-text voice samples; uses server's existing credential loader."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import server as s
from template_answers import SpeechStream

OUT = Path('/data/build/tmp/overnight/E/voices')
TEXT = 'In the last half minute I heard a Northern Cardinal and an American Robin.'


def save(name, pcm):
    with wave.open(str(OUT / name), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(24000)
        f.writeframes(pcm)


async def main():
    import websockets
    for attempt in range(10):
        pcm = b''
        transcript = ''
        async with websockets.connect('wss://api.boson.ai/v1/realtime?model=' + s.MODEL,
                additional_headers={'Authorization': 'Bearer ' + s.api_key()}, max_size=None) as ws:
            await ws.send(json.dumps({'type': 'session.update', 'session': {'model': s.MODEL,
                'output_modalities': ['audio'], 'tools': [], 'tool_choice': 'none',
                'instructions': 'Read the requested sentence verbatim. No additions.',
                'audio': {'output': {'voice': 'default', 'format': {'type': 'audio/pcm', 'rate': 24000}}}}}))
            while True:
                e = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if e['type'] in ('session.created', 'session.updated'):
                    break
            await ws.send(json.dumps({'type': 'conversation.item.create', 'item': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Say exactly: ' + TEXT}]}}))
            await ws.send(json.dumps({'type': 'response.create'}))
            while True:
                e = json.loads(await asyncio.wait_for(ws.recv(), 30))
                if e['type'] in ('response.output_audio.delta', 'response.audio.delta'):
                    pcm += base64.b64decode(e['delta'])
                if e['type'] in ('response.output_audio_transcript.delta', 'response.audio_transcript.delta'):
                    transcript += e.get('delta', '')
                if e['type'] == 'response.done':
                    break
            print(json.dumps({'attempt': attempt, 'status': e['response'].get('status'), 'transcript': transcript, 'bytes': len(pcm)}), flush=True)
            if pcm and transcript.strip() == TEXT:
                save('realtime-default.wav', pcm)
                break
    stream = SpeechStream()
    try:
        for voice in ['default', 'eleanor']:
            pcm = b''
            async for chunk in stream.chunks(TEXT, s.api_key(), voice):
                pcm += chunk
            save('tts-' + voice + '.wav', pcm)
            print(json.dumps({'voice': voice, 'bytes': len(pcm)}), flush=True)
    finally:
        stream.close()


if __name__ == '__main__':
    asyncio.run(main())
