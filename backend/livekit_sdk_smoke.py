"""Opt-in real OpenAI + LiveKit SDK test with local synthetic audio I/O.

No cloud room or physical speaker is involved. Adaptive inference is explicitly
disabled only in this fixture; production uses adaptive mode with Cloud keys.
"""
import asyncio
import argparse
from pathlib import Path
import json
import logging
import sys
import wave

import numpy as np
from scipy.signal import resample_poly
from livekit import rtc
from livekit.agents import utils
from livekit.agents.voice import io

from livekit_voice import create_session, RoutedVoiceAgent
from realtime_webrtc import openai_config
from test_livekit_sdk import MemorySink

ROOT=Path(__file__).resolve().parents[1]


class SyntheticMicrophone(io.AudioInput):
    def __init__(self):
        super().__init__(label='synthetic microphone')
        self.audio=bytearray()
        self.closed=False

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        await asyncio.sleep(.02)
        if self.audio:
            data=bytes(self.audio[:960]);del self.audio[:960]
            data=data.ljust(960,b'\0')
        else:
            data=bytes(960)
        return rtc.AudioFrame(data,sample_rate=24000,num_channels=1,samples_per_channel=480)


async def main(wav_path):
    report={'passed':False,'physical_phone':False,'acoustic_echo_test':False,
            'adaptive_cloud_inference_test':False,'checks':[]}
    output=ROOT/'out/livekit/sdk-smoke.json'
    session=None
    class Errors(logging.Handler):
        def __init__(self):
            super().__init__(logging.ERROR)
            self.count=0
        def emit(self,record):
            self.count+=1
    errors=Errors()
    logging.getLogger('livekit.agents').addHandler(errors)
    try:
        with wave.open(str(wav_path),'rb') as source:
            if source.getnchannels()!=1 or source.getsampwidth()!=2:
                raise ValueError('Expected mono PCM16 WAV')
            samples=np.frombuffer(source.readframes(source.getnframes()),dtype='<i2').astype(np.float32)
            samples=resample_poly(samples,24000,source.getframerate()).clip(-32768,32767).astype('<i2')
        async with utils.http_context.open():
            session=create_session(openai_config(),adaptive=False)
            microphone=SyntheticMicrophone();sink=MemorySink()
            session.input.audio=microphone;session.output.audio=sink
            transcript=asyncio.get_running_loop().create_future()
            @session.on('user_input_transcribed')
            def heard(event):
                if event.is_final and event.transcript.strip() and not transcript.done():
                    transcript.set_result(event.transcript)
            await session.start(RoutedVoiceAgent(),record=False,session_host=False)
            await asyncio.sleep(2)
            microphone.audio.extend(samples.tobytes())
            text=await asyncio.wait_for(transcript,25)
            assert 'hear' in text.lower(), 'Synthetic microphone was not transcribed as expected'
            report['checks'].append({'case':'LiveKit VAD commits synthetic speech for real GPT transcription','passed':True})
            await asyncio.sleep(1)
            assert sink.frames==0, 'The voice model answered before the application authorized it'
            report['checks'].append({'case':'Application hook prevents duplicate or unauthorized automatic speech','passed':True})
            reply=session.generate_reply(user_input='Authoritative backend result: the microphone check succeeded.',
                instructions='Say exactly: Yes, I can hear you.',tool_choice='none')
            await asyncio.wait_for(reply,30)
            assert reply.exception() is None and sink.frames>0
            report['checks'].append({'case':'Approved reply uses real GPT audio and LiveKit playback','passed':True})
            microphone.closed=True
            await session.aclose();session=None
            assert errors.count==0, 'LiveKit logged a runtime error'
            report['passed']=True
    except Exception as error:
        report['error_type']=type(error).__name__
    finally:
        if session:
            await session.aclose()
        logging.getLogger('livekit.agents').removeHandler(errors)
        report['sdk_runtime_errors']=errors.count
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wav',type=Path,default=ROOT/'out/audio-lab/microphone-check.wav')
    arguments=parser.parse_args()
    if not arguments.wav.is_file():
        parser.error('Generate backend/make_audio_lab_fixtures.ps1 first, or provide --wav')
    raise SystemExit(asyncio.run(main(arguments.wav)))
