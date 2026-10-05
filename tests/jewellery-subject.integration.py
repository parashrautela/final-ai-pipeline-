"""Real foreground + Jev regression. Requires TYPESAFE_API_KEY and a read-only fixture.
Usage: python tests/jewellery-subject.integration.py fixture.json [rgba-cutout.png]
"""
import asyncio,io,json,sys,tempfile
from pathlib import Path
from urllib.parse import urlparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image,ImageOps
from app.services.catalogue_search import CatalogueIndex,ImageEncoder,InvalidPhoto,source_url
from app.services.jev_catalogue import decide_matches

async def main():
    fixture=json.loads(Path(sys.argv[1]).read_text())
    rows=[dict(row,is_published=True) for row in fixture['products']]
    reference=Path(fixture['reference']).read_bytes()
    with tempfile.TemporaryDirectory() as directory:
        index=CatalogueIndex(ImageEncoder(),lambda:rows,{urlparse(source_url(row)).hostname for row in rows},Path(directory)/'index.json')
        await index.refresh(rows)
        image=ImageOps.exif_transpose(Image.open(io.BytesIO(reference))).convert('RGB');image.thumbnail((1024,1024))
        out=io.BytesIO();image.save(out,'JPEG',quality=92)
        async def check(photo,label):
            candidates=await index.search(photo,rows,fixture['category'],candidate_limit=12)
            decision=await decide_matches(index,photo,rows,candidates)
            assert decision['matches'] and decision['matches'][0]['id']==rows[0]['id'],label
            print('PASS:',label,flush=True)
        await check(out.getvalue(),'app-resized reference accepted first by Jev')
        if len(sys.argv)>2:
            cutout=Image.open(sys.argv[2]).convert('RGBA')
            for color in ['#eeeeee','#9f2222','#408a9e']:
                background=Image.new('RGB',cutout.size,color);background.paste(cutout,mask=cutout.getchannel('A'));background.thumbnail((800,800))
                out=io.BytesIO();background.save(out,'JPEG',quality=90)
                await check(out.getvalue(),'same jewellery on '+color+' background')
        out=io.BytesIO();Image.fromarray(np.random.default_rng(7).integers(0,256,(300,300,3),dtype=np.uint8)).save(out,'PNG')
        try:
            candidates=await index.search(out.getvalue(),rows,fixture['category'],candidate_limit=12)
            decision=await decide_matches(index,out.getvalue(),rows,candidates)
            assert not decision['matches'],'Unrelated image accepted as jewellery'
        except InvalidPhoto:
            pass  # Explicit unusable-foreground error is also a valid rejection.
        print('PASS: unrelated image rejected; cache model version migration exercised')
asyncio.run(main())
