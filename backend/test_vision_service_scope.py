"""Numerical scope tests in .venv-vision, without loading model weights."""
import base64
import importlib.util
import io
from types import SimpleNamespace
import unittest
from PIL import Image


@unittest.skipUnless(importlib.util.find_spec('torch'), 'Run this module in .venv-vision')
class ScopeTests(unittest.TestCase):
    def setUp(self):
        import torch
        from vision_service import VisionEngine
        self.torch=torch
        self.engine=VisionEngine.__new__(VisionEngine)
        self.engine.torch=torch
        self.engine.names=['Fungus parasiticus','Pinus strobus','Acer rubrum','Apis mellifera','Anas platyrhynchos']
        self.engine.groups=['Fungi','Plantae','Plantae','Insecta','Aves']
        self.engine.name_indices={name:[i] for i,name in enumerate(self.engine.names)}
        self.engine.group_indices={g:torch.tensor([i for i,v in enumerate(self.engine.groups) if g==v],dtype=torch.long)
            for g in self.engine.groups}
        self.engine.tree=SimpleNamespace(txt_embeddings=torch.zeros(1,5),
            create_image_features=lambda _:None,
            create_probabilities=lambda *_:torch.tensor([[.6,.25,.10,.04,.01]]))
        stream=io.BytesIO();Image.new('RGB',(32,32),'green').save(stream,format='JPEG')
        self.encoded=base64.b64encode(stream.getvalue()).decode()

    def classify(self,**kwargs):
        from vision_service import FrameRequest
        return self.engine._classify(FrameRequest(jpeg_b64=self.encoded,**kwargs))

    def test_scope_uses_all_group_members_and_preserves_original_probability(self):
        r=self.classify(target_group='Plantae')
        self.assertEqual([x['sci'] for x in r['candidates']],['Pinus strobus','Acer rubrum'])
        self.assertAlmostEqual(r['candidates'][0]['p'],.25/.35,places=5)
        self.assertAlmostEqual(r['candidates'][0]['global_p'],.25)
        self.assertEqual(r['open_top'][0]['group'],'Fungi')
        self.assertTrue(r['assessment']['category_conflict'])
        self.assertTrue(r['assessment']['weak_visual_evidence'])

    def test_local_list_is_intersected_with_group_and_never_falls_open(self):
        r=self.classify(target_group='Plantae',candidates=['Apis mellifera','Pinus strobus'])
        self.assertEqual(len(r['candidates']),1)
        self.assertEqual(r['candidates'][0]['sci'],'Pinus strobus')
        self.assertEqual(r['candidates'][0]['p'],1)
        self.assertAlmostEqual(r['candidates'][0]['global_p'],.25)
        r=self.classify(target_group='Plantae',candidates=['Apis mellifera'])
        self.assertEqual(r['candidates'],[])
        self.assertTrue(r['assessment']['weak_visual_evidence'])

    def test_unknown_target_remains_open_and_has_no_category_conflict(self):
        r=self.classify()
        self.assertEqual(r['candidates'][0]['sci'],'Fungus parasiticus')
        self.assertAlmostEqual(r['candidates'][0]['p'],.6)
        self.assertFalse(r['assessment']['category_conflict'])


if __name__=='__main__':unittest.main()
