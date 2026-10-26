import unittest
from playout_evidence import received_speech_frames


class PlayoutEvidenceTests(unittest.TestCase):
    def test_earlier_receipt_audio_cannot_prove_the_result_was_received(self):
        events = [{'type':'state','speaking':True,'_at':1},
                  {'type':'state','speaking':False,'_at':2},
                  {'type':'state','speaking':True,'_at':4},
                  {'type':'state','speaking':False,'_at':6}]
        self.assertEqual(received_speech_frames(events,[1.1,1.5],6.01),0)
        self.assertEqual(received_speech_frames(events,[1.1,4.1,5.8],6.01),2)

    def test_duplicate_speaking_states_do_not_restart_the_audio_window(self):
        events = [{'type':'state','speaking':True,'_at':1},
                  {'type':'state','speaking':True,'_at':2},
                  {'type':'state','speaking':False,'_at':3}]
        self.assertEqual(received_speech_frames(events,[1.1,1.8,2.5],3.01),3)

    def test_later_unrelated_audio_does_not_count_for_an_earlier_answer(self):
        events = [{'type':'state','speaking':True,'_at':1},
                  {'type':'state','speaking':False,'_at':2},
                  {'type':'state','speaking':True,'_at':5}]
        self.assertEqual(received_speech_frames(events,[5.1,5.5],2.01),0)
