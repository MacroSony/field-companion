"""Opt-in loopback-only server for real WebRTC media with synthetic app evidence.

Never deploy this fixture server. BirdNET/taxonomy/research are fixtures; the
OpenAI WebRTC media, transcription, sideband routing and tools are real.
"""
import argparse
import asyncio
import logging

import server as s


async def no_birdnet(self, *args):
    pass


async def fixture_research(self, question, snapshot, progress):
    await progress('retrieving')
    await asyncio.sleep(5)
    return {'ok': True, 'answer': 'The fixture investigation is complete. No species research was performed.'}


if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8803)
    args = parser.parse_args()
    s.app.state.enable_webrtc = True
    s.Session.bird_window = no_birdnet
    s.Session.allowed_species = lambda self: {'Anas platyrhynchos', 'Haliaeetus leucocephalus'}
    s.Session.run_investigation = fixture_research
    original_init = s.Session.__init__
    def seeded(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        bird = s.Observation('fixture-eagle', fused=[{'sci': 'Haliaeetus leucocephalus', 'en': 'Bald Eagle', 'p': .9}],
            uncertain=False, analysis_status='ready', analysis_stage='ready')
        self.observations[bird.id] = bird
        self.focus = bird.id
    s.Session.__init__ = seeded
    s.resolve = lambda name, group='Aves': {'sci': 'Anas platyrhynchos', 'en': 'Mallard', 'zh': '绿头鸭', 'photo': '', 'group': 'Aves'}
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.getLogger('websockets').setLevel(logging.WARNING)
    uvicorn.run(s.app, host='127.0.0.1', port=args.port, log_level='warning')
