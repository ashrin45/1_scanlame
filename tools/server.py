#!/usr/bin/env python3
"""Serveur de reception pour la page de capture (FastAPI).

Lancer :  pip install fastapi uvicorn python-multipart
          python tools/server.py --root ./series --port 8000

Sur le telephone, mettre l'URL du serveur dans la page. Les photos et les morceaux de video arrivent
au fil de l'eau et sont ecrits directement sur disque ; rien ne s'accumule sur le telephone.

Acces depuis le telephone :
- USB : `adb reverse tcp:8000 tcp:8000`, puis URL http://localhost:8000 (Chrome accepte localhost sans HTTPS)
- Wi-Fi du labo : la page est en HTTPS, donc le serveur doit l'etre aussi (certificat local, ou tunnel
  type cloudflared) sinon Chrome bloque la requete (contenu mixte).

Arborescence : series/<serie>/video.<ext> | photos/*.jpg | serie.json | journal.txt
"""
import argparse, json, os
from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

ap = argparse.ArgumentParser()
ap.add_argument('--root', default='series')
ap.add_argument('--port', type=int, default=8000)
ap.add_argument('--host', default='0.0.0.0')
args = ap.parse_args()
os.makedirs(args.root, exist_ok=True)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

def sdir(series: str) -> str:
    safe = ''.join(c for c in series if c.isalnum() or c in '_-') or 'serie'
    d = os.path.join(args.root, safe)
    os.makedirs(d, exist_ok=True)
    return d

@app.post('/photo')
async def photo(file: UploadFile = File(...), series: str = Form('serie'), t_ms: str = Form('0')):
    d = os.path.join(sdir(series), 'photos'); os.makedirs(d, exist_ok=True)
    name = os.path.basename(file.filename or 'photo.jpg')
    with open(os.path.join(d, name), 'wb') as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    return {'ok': True, 'name': name}

@app.post('/chunk')
async def chunk(file: UploadFile = File(...), series: str = Form(...), seq: int = Form(...), mime: str = Form('video/webm')):
    ext = 'mp4' if 'mp4' in mime else 'webm'
    path = os.path.join(sdir(series), f'video.{ext}')
    # les morceaux arrivent dans l'ordre (un fetch par seconde) ; on ajoute a la fin du fichier
    with open(path, 'ab') as f:
        while data := await file.read(1 << 20):
            f.write(data)
    return {'ok': True, 'seq': seq, 'size': os.path.getsize(path)}

@app.post('/finish')
async def finish(request: Request):
    meta = await request.json()
    d = sdir(meta.get('series', 'serie'))
    json.dump(meta, open(os.path.join(d, 'serie.json'), 'w'), indent=1)
    return {'ok': True, 'dir': d}

@app.get('/')
def index():
    return JSONResponse({'ok': True, 'series': sorted(os.listdir(args.root))})

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
