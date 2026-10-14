#!/usr/bin/env python3
"""Application du Space Hugging Face : reception des prises, assemblage en tache de fond, publication GitHub.

Variables (secrets du Space) :
  SCANLAME_TOKEN  jeton partage avec la page de capture (obligatoire)
  GH_TOKEN        jeton GitHub (fine-grained, contenu lecture/ecriture sur le depot) pour publier les lames
  GH_REPO         depot cible, par defaut ashrin45/1_scanlame
  KEEP_RAW        "1" pour garder les prises brutes apres assemblage (sinon supprimees)
Routes :
  GET  /            etat (HTML)      GET /capture  page de capture pre-reglee sur ce serveur
  POST /photo /chunk /finish (X-Token)   GET /series (X-Token)   POST /process/{serie} (X-Token)
  GET  /result/{serie}/pano.jpg  apercu de la mosaique
"""
import json, os, queue, shutil, subprocess, threading, time, html
from fastapi import FastAPI, File, Form, UploadFile, Request, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('SCANLAME_DATA', '/tmp/series')
TOKEN = os.environ.get('SCANLAME_TOKEN', '')
GH_TOKEN = os.environ.get('GH_TOKEN', '')
GH_REPO = os.environ.get('GH_REPO', 'ashrin45/1_scanlame')
KEEP_RAW = os.environ.get('KEEP_RAW', '0') == '1'
os.makedirs(DATA, exist_ok=True)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])
jobs = {}            # serie -> dict(state, started, ended, log, url)
q = queue.Queue()

def auth(x_token: str = Header(default='')):
    if not TOKEN or x_token != TOKEN:
        raise HTTPException(401, 'jeton invalide')

def sdir(series: str) -> str:
    safe = ''.join(c for c in series if c.isalnum() or c in '_-') or 'serie'
    d = os.path.join(DATA, safe); os.makedirs(d, exist_ok=True); return d

# ------------------------------------------------------------------ reception
@app.post('/photo', dependencies=[Depends(auth)])
async def photo(file: UploadFile = File(...), series: str = Form('serie'), t_ms: str = Form('0')):
    d = os.path.join(sdir(series), 'photos'); os.makedirs(d, exist_ok=True)
    name = os.path.basename(file.filename or 'photo.jpg')
    with open(os.path.join(d, name), 'wb') as f:
        while chunk := await file.read(1 << 20): f.write(chunk)
    return {'ok': True, 'name': name}

@app.post('/chunk', dependencies=[Depends(auth)])
async def chunk(file: UploadFile = File(...), series: str = Form(...), seq: int = Form(...), mime: str = Form('video/webm')):
    path = os.path.join(sdir(series), 'video.' + ('mp4' if 'mp4' in mime else 'webm'))
    with open(path, 'ab') as f:
        while data := await file.read(1 << 20): f.write(data)
    return {'ok': True, 'seq': seq, 'size': os.path.getsize(path)}

@app.post('/finish', dependencies=[Depends(auth)])
async def finish(request: Request):
    meta = await request.json()
    series = meta.get('series', 'serie'); d = sdir(series)
    json.dump(meta, open(os.path.join(d, 'serie.json'), 'w'), indent=1)
    enqueue(os.path.basename(d), meta.get('title'))
    return {'ok': True, 'queued': True}

@app.post('/process/{series}', dependencies=[Depends(auth)])
def process(series: str, title: str = ''):
    if not os.path.isdir(os.path.join(DATA, series)): raise HTTPException(404, 'série inconnue')
    enqueue(series, title or None); return {'ok': True}

@app.get('/series', dependencies=[Depends(auth)])
def series_list():
    out = []
    for name in sorted(os.listdir(DATA)):
        d = os.path.join(DATA, name)
        size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(d) for f in fs)
        out.append({'series': name, 'mb': round(size / 1048576, 1), 'job': {k: v for k, v in jobs.get(name, {}).items() if k != 'log'}})
    return out

# ------------------------------------------------------------------ traitement
def enqueue(series, title=None):
    if jobs.get(series, {}).get('state') in ('queued', 'running'): return
    jobs[series] = dict(state='queued', started=None, ended=None, log='', url=None, title=title or series)
    q.put(series)

def run(cmd, cwd=None, env=None):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)[-4000:]

def worker():
    while True:
        series = q.get()
        j = jobs[series]; j.update(state='running', started=time.time())
        d = os.path.join(DATA, series); res = os.path.join(d, 'result')
        try:
            rc, out = run(['python', os.path.join(ROOT, 'tools', 'pipeline.py'), d, '--out', res])
            j['log'] += out
            if rc != 0: raise RuntimeError('pipeline en échec')
            j['url'] = publish(series, j['title'], res)
            j['state'] = 'done'
            if not KEEP_RAW:
                for f in os.listdir(d):
                    if f.startswith('video.') or f == 'photos': (shutil.rmtree if f == 'photos' else os.remove)(os.path.join(d, f))
                shutil.rmtree(os.path.join(res, 'frames'), ignore_errors=True); shutil.rmtree(os.path.join(res, 'full'), ignore_errors=True)
        except Exception as e:
            j['state'] = 'error'; j['log'] += f'\nERREUR {e}'
        j['ended'] = time.time()
        q.task_done()

def publish(series, title, res):
    if not GH_TOKEN: raise RuntimeError('GH_TOKEN absent : mosaïque produite mais non publiée')
    work = os.path.join('/tmp', 'pub_' + series)
    shutil.rmtree(work, ignore_errors=True)
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    url = f'https://x-access-token:{GH_TOKEN}@github.com/{GH_REPO}.git'
    rc, out = run(['git', 'clone', '--depth', '1', '--filter=blob:none', url, work], env=env)
    if rc: raise RuntimeError('clone GitHub : ' + out.replace(GH_TOKEN, '***'))
    slug = series.lower().replace('_', '-')
    rc, out = run(['python', os.path.join(work, 'tools', 'publish_lame.py'), os.path.join(res, 'pano.jpg'), '--name', slug,
                   '--title', title or series, '--fields', os.path.join(res, 'fields.json')])
    if rc: raise RuntimeError('publish_lame : ' + out)
    run(['git', '-c', 'user.name=scanlame', '-c', 'user.email=scanlame@users.noreply.github.com', 'add', 'lames'], cwd=work)
    rc, out = run(['git', '-c', 'user.name=scanlame', '-c', 'user.email=scanlame@users.noreply.github.com', 'commit', '-q', '-m', f'Lame {slug}'], cwd=work)
    rc, out = run(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=work, env=env)
    if rc: raise RuntimeError('push GitHub : ' + out.replace(GH_TOKEN, '***'))
    shutil.rmtree(work, ignore_errors=True)
    owner, repo = GH_REPO.split('/')
    return f'https://{owner}.github.io/{repo}/lames/{slug}/'

threading.Thread(target=worker, daemon=True).start()

# ------------------------------------------------------------------ pages
@app.get('/capture')
def capture():
    return FileResponse(os.path.join(ROOT, 'index.html'), media_type='text/html')

@app.get('/result/{series}/pano.jpg')
def result(series: str):
    p = os.path.join(DATA, os.path.basename(series), 'result', 'pano.jpg')
    if not os.path.isfile(p): raise HTTPException(404)
    return FileResponse(p, media_type='image/jpeg')

@app.get('/', response_class=HTMLResponse)
def index():
    rows = []
    for name in sorted(os.listdir(DATA), reverse=True):
        j = jobs.get(name, {}); st = j.get('state', 'reçue')
        link = f'<a href="{j["url"]}">voir la lame</a>' if j.get('url') else (f'<a href="/result/{name}/pano.jpg">aperçu</a>' if os.path.isfile(os.path.join(DATA, name, 'result', 'pano.jpg')) else '')
        rows.append(f'<tr><td>{html.escape(name)}</td><td class="s-{st}">{st}</td><td>{link}</td><td><details><summary>journal</summary><pre>{html.escape(j.get("log", "")[-3000:])}</pre></details></td></tr>')
    return f'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Scan lame</title>
<style>body{{font:15px/1.4 system-ui,sans-serif;margin:0;padding:20px;background:#17131b;color:#ece6f0}}a{{color:#c58ad6}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #3a323f;padding:8px 6px;text-align:left;vertical-align:top}}
.s-done{{color:#6fcf97}}.s-running{{color:#f2c94c}}.s-error{{color:#eb5757}}pre{{white-space:pre-wrap;font-size:11px;color:#a79cb1;max-height:40vh;overflow:auto}}</style></head><body>
<h1>Scan lame</h1><p>Récepteur et assembleur. Page de capture : <a href="/capture">/capture</a>. Jeton requis pour envoyer. {'Publication GitHub active' if GH_TOKEN else 'GH_TOKEN absent : pas de publication'}.</p>
<table><tr><th>Série</th><th>État</th><th>Résultat</th><th></th></tr>{''.join(rows) or '<tr><td colspan="4">aucune série reçue</td></tr>'}</table></body></html>'''
