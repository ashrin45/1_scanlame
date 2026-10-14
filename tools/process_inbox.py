#!/usr/bin/env python3
"""Traite les prises deposees dans le bucket Supabase « series » : pour chaque serie close (serie.json present),
telecharge les morceaux video (ou les photos), assemble (tools/pipeline.py), publie (tools/publish_lame.py)
puis supprime la serie du bucket. Lance par GitHub Actions ; utilisable aussi a la main.

Env : SUPABASE_URL, SUPABASE_SERVICE_KEY. Options : --keep (ne pas supprimer du bucket), --only <serie>
"""
import argparse, json, os, shutil, subprocess, sys, time
import requests

ap = argparse.ArgumentParser()
ap.add_argument('--keep', action='store_true')
ap.add_argument('--only', default=None)
ap.add_argument('--work', default='inbox')
a = ap.parse_args()

URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
if not URL or not KEY:
    sys.exit('SUPABASE_URL / SUPABASE_SERVICE_KEY manquants')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
BUCKET = 'series'

def ls(prefix):
    r = requests.post(f'{URL}/storage/v1/object/list/{BUCKET}', headers=H, json={'prefix': prefix, 'limit': 1000, 'sortBy': {'column': 'name', 'order': 'asc'}})
    r.raise_for_status(); return r.json()
def get(path, dest):
    with requests.get(f'{URL}/storage/v1/object/{BUCKET}/{path}', headers=H, stream=True) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for c in r.iter_content(1 << 20): f.write(c)
def put_text(path, text):
    r = requests.post(f'{URL}/storage/v1/object/{BUCKET}/{path}', headers={**H, 'Content-Type': 'text/plain', 'x-upsert': 'true'}, data=text.encode()); r.raise_for_status()
def slugify(t):
    import unicodedata, re
    t = unicodedata.normalize('NFKD', t).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', t).strip('-')[:40]
def rm(paths):
    for i in range(0, len(paths), 100):
        r = requests.delete(f'{URL}/storage/v1/object/{BUCKET}', headers=H, json={'prefixes': paths[i:i + 100]}); r.raise_for_status()

series = [e['name'] for e in ls('') if e.get('id') is None or e.get('metadata') is None]   # dossiers
series = [s for s in series if not a.only or s == a.only]
done = []
for s in series:
    entries = ls(s)
    names = {e['name'] for e in entries}
    if 'serie.json' not in names:
        print(f'{s} : pas encore close, ignorée'); continue
    if 'failed.txt' in names:
        print(f'{s} : déjà en échec, ignorée'); continue
    print(f'== {s}')
    d = os.path.join(a.work, s); shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    get(f'{s}/serie.json', os.path.join(d, 'serie.json'))
    meta = json.load(open(os.path.join(d, 'serie.json')))
    paths = [f'{s}/serie.json']
    chunks = sorted(e['name'] for e in ls(f'{s}/chunks'))
    if chunks:
        ext = 'mp4' if 'mp4' in str(meta.get('video_mime', '')) else 'webm'
        with open(os.path.join(d, f'video.{ext}'), 'wb') as out:
            for c in chunks:
                tmp = os.path.join(d, 'tmp.bin'); get(f'{s}/chunks/{c}', tmp)
                with open(tmp, 'rb') as f: shutil.copyfileobj(f, out)
                paths.append(f'{s}/chunks/{c}')
        os.remove(tmp)
        print(f'  vidéo : {len(chunks)} morceaux, {os.path.getsize(os.path.join(d, f"video.{ext}")) >> 20} Mo')
    photos = sorted(e['name'] for e in ls(f'{s}/photos'))
    if photos:
        os.makedirs(os.path.join(d, 'photos'))
        for p in photos:
            get(f'{s}/photos/{p}', os.path.join(d, 'photos', p)); paths.append(f'{s}/photos/{p}')
        print(f'  photos : {len(photos)}')
    vid = os.path.join(d, f'video.{ext}') if chunks else None
    if (not chunks and not photos) or (vid and os.path.getsize(vid) < 200_000):
        print('  vide ou trop courte : supprimée'); rm(paths); continue
    t0 = time.time()
    res = os.path.join(d, 'result')
    rc = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'pipeline.py'), d, '--out', res]).returncode
    if rc != 0:
        print(f'  ÉCHEC du pipeline (code {rc}), série marquée en échec dans le bucket')
        logp = os.path.join(res, 'pipeline.log')
        put_text(f'{s}/failed.txt', open(logp).read()[-4000:] if os.path.isfile(logp) else f'pipeline code {rc}'); continue
    title = meta.get('title') or s
    slug = (slugify(title) + '-' + s.split('_')[1][4:] + s.split('_')[2][:4]) if meta.get('title') and s.count('_') >= 2 else s.lower().replace('_', '-')
    rc = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'publish_lame.py'), os.path.join(res, 'pano.jpg'), '--name', slug,
                         '--title', title, '--fields', os.path.join(res, 'fields.json')]).returncode
    if rc != 0:
        print('  ÉCHEC de la publication'); put_text(f'{s}/failed.txt', 'publication'); continue
    print(f'  publiée : lames/{slug}/ en {time.time() - t0:.0f} s')
    if not a.keep: rm(paths)
    done.append(slug)
    shutil.rmtree(d, ignore_errors=True)
print('séries publiées :', done or 'aucune')
if done:
    open(os.environ.get('GITHUB_OUTPUT', '/dev/null'), 'a').write('published=' + ','.join(done) + '\n')
