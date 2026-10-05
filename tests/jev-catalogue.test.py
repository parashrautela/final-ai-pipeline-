import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import numpy as np
from PIL import Image
from app.services import jev_catalogue as jev
from app.services.catalogue_search import CatalogueIndex, cache_key, visual_fingerprint

class Decisions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        data=io.BytesIO();Image.new('RGB',(40,40),'white').save(data,'JPEG');self.photo=data.getvalue()
        self.rows=[dict(id='a',is_published=True,jewellery_type='necklace',image_url='https://catalogue.test/a.jpg'),dict(id='b',is_published=True,jewellery_type='necklace',image_url='https://catalogue.test/b.jpg')]
        self.index=SimpleNamespace(fingerprints={cache_key(row):visual_fingerprint(self.photo) for row in self.rows},schedule_refresh=lambda:None)
        self.result=dict(matches=[dict(id='a',similarity=.85),dict(id='b',similarity=.99)],checked=2,total=2,skipped=0)
    def answer(self,choice):
        return dict(type='choice',choice=choice,probabilities={key:1.0 if key==choice else 0.0 for key in jev.CRITERIA},confidence=1.0)
    async def decide(self,answers,status=200):
        requests=[];original=httpx.AsyncClient
        def handler(request):
            requests.append(request)
            return httpx.Response(status,json=dict(model='jev-1.13.0',answers=answers))
        with patch.dict('os.environ',{'TYPESAFE_API_KEY':'test-only-key'}),patch.object(jev.httpx,'AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw)):
            result=await jev.decide_matches(self.index,self.photo,self.rows,self.result)
        return result,requests
    async def test_jev_controls_acceptance_below_old_cutoff(self):
        result,requests=await self.decide({'pair_0':self.answer('similar'),'pair_1':self.answer('different')})
        self.assertEqual([m['id'] for m in result['matches']],['a']);self.assertEqual(result['matches'][0]['similarity'],.85)
        self.assertEqual(result['decision_source'],'jev');self.assertEqual(len(requests),1)
        body=requests[0].content.decode();self.assertNotIn('catalogue.test',body);self.assertNotIn('pixels_sha256',body)
        self.assertEqual(requests[0].headers['authorization'],'Bearer test-only-key')
    async def test_uncertain_is_not_displayed_as_similar(self):
        result,_=await self.decide({'pair_0':self.answer('uncertain'),'pair_1':self.answer('uncertain')})
        self.assertFalse(result['matches']);self.assertEqual(len(result['decisions']),2)
    async def test_incomplete_response_and_service_failure_never_fallback(self):
        for answers,status in [({'pair_0':self.answer('similar')},200),({},401),({},429)]:
            with self.assertRaises(LookupError):await self.decide(answers,status)
    async def test_bad_probabilities_rejected(self):
        answer=self.answer('similar');answer['probabilities']['similar']=float('nan')
        with self.assertRaises(ValueError):jev.parse_answer(answer)
    async def test_missing_key_and_missing_evidence_are_retryable(self):
        with patch.dict('os.environ',{'TYPESAFE_API_KEY':''}):
            with self.assertRaises(LookupError):await jev.decide_matches(self.index,self.photo,self.rows,self.result)
        self.index.fingerprints={}
        with patch.dict('os.environ',{'TYPESAFE_API_KEY':'test-only-key'}):
            with self.assertRaises(LookupError):await jev.decide_matches(self.index,self.photo,self.rows,self.result)
    async def test_candidate_stage_does_not_drop_below_old_cutoff(self):
        vector=np.zeros(512,dtype=np.float32);vector[0]=1
        index=CatalogueIndex(SimpleNamespace(embed=lambda photo:vector),lambda:self.rows,set(),Path('/private/tmp/nonexistent-jev-test-index.json'))
        for row,score in zip(self.rows,[.85,.99]):
            v=np.zeros(512,dtype=np.float32);v[0]=score;v[1]=(1-score**2)**.5;index.vectors[cache_key(row)]=v
        candidates=await index.search(self.photo,self.rows,'necklace',candidate_limit=12)
        self.assertEqual(len(candidates['matches']),2)
        old=await index.search(self.photo,self.rows,'necklace');self.assertEqual(len(old['matches']),1)
    async def test_fingerprint_cache_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'index.json'
            index=CatalogueIndex(None,None,set(),path)
            vector=np.zeros(512);vector[0]=1
            index.fingerprints=self.index.fingerprints.copy()
            index.persist({cache_key(self.rows[0]):vector.tolist()})
            restored=CatalogueIndex(None,None,set(),path)
            self.assertEqual(restored.fingerprints[cache_key(self.rows[0])],self.index.fingerprints[cache_key(self.rows[0])])

if __name__=='__main__':unittest.main()
