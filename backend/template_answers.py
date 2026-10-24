"""Deterministic local-data answers and cancellable, connection-pooled REST PCM."""
import asyncio
import re
import threading

import requests

_SMALL = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
_TENS = 'zero ten twenty thirty forty fifty sixty seventy eighty ninety'.split()


def number_words(n):
    n = int(n)
    if n < 0:
        return 'minus ' + number_words(-n)
    if n < 20:
        return _SMALL[n]
    if n < 100:
        return _TENS[n // 10] + ('-' + _SMALL[n % 10] if n % 10 else '')
    for size, name in ((10**12, 'trillion'), (10**9, 'billion'), (10**6, 'million'), (1000, 'thousand'), (100, 'hundred')):
        if n >= size:
            return number_words(n // size) + ' ' + name + (' ' + number_words(n % size) if n % size else '')


def expand_numbers(text):
    return re.sub(r'\d+(?:\.\d+)?', lambda m: ' point '.join(
        [number_words(m[0].split('.')[0])] + [' '.join(number_words(int(c)) for c in m[0].split('.')[1])] if '.' in m[0] else [number_words(m[0])]), text)


def joined(names):
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]


def build_answer(name, result):
    if result.get('ok') is False:
        return None
    if name == 'recent_sounds':
        seconds = result['seconds']
        window = 'the last half minute' if seconds == 30 else 'the last ' + format(seconds, 'g') + (' second' if seconds == 1 else ' seconds')
        names = list(dict.fromkeys(d.get('en') or d['sci'] for d in result['detections']))
        text = ('The detector picked up ' + joined(names[:3]) + (f' and {len(names)-3} more species' if len(names) > 3 else '') + ' in ' + window + '; these are unconfirmed.' if names else 'Nothing new in ' + window + '.')
    elif name == 'get_checklist':
        items = result['items']
        if not items:
            return 'No species logged so far.'
        names = [r.get('en') or r['sci'] for r in items]
        count = sum(r['count'] for r in items)
        text = f'{len(items)} species logged so far: ' + joined(names[:3]) + (f' and {len(names)-3} more' if len(names) > 3 else '') + f'. {count} ' + ('individual' if count == 1 else 'individuals') + ' in total.'
    else:
        return None
    return expand_numbers(text[0].upper() + text[1:])


class SpeechStream:
    """One reusable HTTP pool per field session; worker never touches playback state."""
    def __init__(self):
        self.http = requests.Session()
        self.lock = threading.Lock()

    async def chunks(self, text, key, voice='default'):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        stop = threading.Event()

        def publish(value):
            if not stop.is_set() and not loop.is_closed():
                loop.call_soon_threadsafe(queue.put_nowait, value)

        def produce():
            try:
                with self.lock:
                    if stop.is_set():
                        return
                    with self.http.post('https://api.boson.ai/v1/audio/speech',
                            headers={'Authorization': 'Bearer ' + key},
                            json={'model': 'higgs-tts-3', 'input': text, 'voice': voice,
                                  'stream': True, 'response_format': 'pcm'},
                            stream=True, timeout=(3, 3)) as response:
                        response.raise_for_status()
                        while not stop.is_set():
                            chunk = response.raw.read1(4096)
                            if not chunk:
                                break
                            publish(chunk)
                publish(None)
            except Exception as exc:
                publish(exc)

        worker = asyncio.create_task(asyncio.to_thread(produce))
        pending = b''
        try:
            async with asyncio.timeout(20):
                while True:
                    chunk = await asyncio.wait_for(queue.get(), 3)
                    if isinstance(chunk, Exception):
                        raise chunk
                    if chunk is None:
                        if pending:
                            raise ValueError('Truncated PCM sample')
                        break
                    pending += chunk
                    end = len(pending) // 2 * 2
                    if end:
                        yield pending[:end]
                        pending = pending[end:]
        finally:
            stop.set()
            # The blocking reader has a three-second socket timeout and owns its
            # response context, so cancellation cannot leave a live stream behind.
            worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    def close(self):
        self.http.close()
