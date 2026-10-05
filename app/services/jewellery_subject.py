"""Local foreground isolation before jewellery feature extraction."""
import hashlib
import io
import os
import threading
from pathlib import Path
import httpx
import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps

SUBJECT_SHA256 = '309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8'
SUBJECT_URL = 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx'
class SubjectNotFound(ValueError): pass

class JewellerySubject:
    def __init__(self):
        self.path = Path(os.getenv('JEWELLERY_SUBJECT_MODEL_PATH', str(Path.home()/'.cache/jewel-image-search/u2netp.onnx')))
        self.session = None
        self.lock = threading.Lock()
    def prepare(self):
        if not self.path.exists() or hashlib.sha256(self.path.read_bytes()).hexdigest() != SUBJECT_SHA256:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.path.with_suffix('.download')
            try:
                with httpx.stream('GET',SUBJECT_URL,follow_redirects=True,timeout=90) as response:
                    response.raise_for_status();count=0;digest=hashlib.sha256()
                    with temporary.open('wb') as file:
                        for chunk in response.iter_bytes():
                            count+=len(chunk)
                            if count>6*1024*1024:raise RuntimeError('Subject model too large')
                            digest.update(chunk);file.write(chunk)
                    if digest.hexdigest()!=SUBJECT_SHA256:raise RuntimeError('Subject model checksum mismatch')
                    temporary.replace(self.path)
            finally:temporary.unlink(missing_ok=True)
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        self.session=ort.InferenceSession(str(self.path),sess_options=options,providers=['CPUExecutionProvider'])
    def isolate(self, photo: bytes):
        with Image.open(io.BytesIO(photo)) as source:
            if source.width*source.height>50_000_000 or max(source.size)>32*min(source.size):
                raise SubjectNotFound('Choose a smaller, clear jewellery photo.')
            image=ImageOps.exif_transpose(source).convert('RGB');image.thumbnail((1024,1024))
        pixels=np.asarray(image.resize((320,320),Image.Resampling.LANCZOS),dtype=np.float32)
        pixels/=max(float(pixels.max()),1e-6)
        pixels=(pixels-np.array([.485,.456,.406],dtype=np.float32))/np.array([.229,.224,.225],dtype=np.float32)
        with self.lock:
            if self.session is None:self.prepare()
            prediction=self.session.run(None,{self.session.get_inputs()[0].name:pixels.transpose(2,0,1)[None]})[0][0,0]
        spread=float(prediction.max()-prediction.min())
        if not np.isfinite(prediction).all() or spread<1e-6:raise SubjectNotFound('Couldn’t isolate the jewellery. Try a clearer photo.')
        prediction=(prediction-prediction.min())/spread
        mask=Image.fromarray((prediction*255).astype('uint8')).resize(image.size,Image.Resampling.LANCZOS)
        hard=mask.point(lambda p:255 if p>96 else 0)
        box=hard.getbbox();coverage=float((np.asarray(mask)>96).mean())
        if box is None or coverage<.002 or coverage>.95:raise SubjectNotFound('Couldn’t isolate the jewellery. Try a photo with the whole piece visible.')
        x0,y0,x1,y1=box;pad=max(3,int(max(x1-x0,y1-y0)*.04))
        box=(max(0,x0-pad),max(0,y0-pad),min(image.width,x1+pad),min(image.height,y1+pad))
        image=image.crop(box);mask=mask.crop(box)
        # Preserve holes and fine-chain soft edges; neutralize the background.
        neutral=Image.new('RGB',image.size,(240,240,240));neutral.paste(image,mask=mask)
        side=max(neutral.size);canvas=Image.new('RGB',(side,side),(240,240,240))
        canvas.paste(neutral,((side-neutral.width)//2,(side-neutral.height)//2));canvas=canvas.resize((512,512),Image.Resampling.LANCZOS)
        output=io.BytesIO();canvas.save(output,'PNG')
        return output.getvalue(), {'foreground_fraction':round(coverage,4),'subject_model':'u2netp','target':'isolated jewellery foreground'}

subject = JewellerySubject()
if __name__=='__main__':subject.prepare()
