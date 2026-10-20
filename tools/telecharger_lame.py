#!/usr/bin/env python3
"""Telecharge une lame de la galerie en pleine resolution, pour l'analyse hors ligne (CNN, QuPath, ImageJ...).

  python tools/telecharger_lame.py <slug> [-o lame.jpg] [--patches 512] [--champs] [--galerie URL] [--local]

- sans option : reconstruit l'image entiere a partir des tuiles Deep Zoom et l'ecrit en JPEG (ou PNG/TIFF selon l'extension)
- --patches N : ecrit aussi un dossier <slug>_patches/ de vignettes N x N px (sans recouvrement) + patches.csv (x, y, fraction de fond),
                en sautant les vignettes vides ; c'est le format le plus simple a donner a un reseau de classification
- --champs    : decoupe aussi chaque photo d'origine (fields.json) en <slug>_champs/<id>.jpg
- --local     : lit les tuiles dans lames/<slug>/ de ce depot au lieu de la galerie en ligne
Les coordonnees des patches et des champs sont en pixels de la lame, les memes que dans la visionneuse et dans lame.json
(um_per_px donne l'echelle quand la lame est calibree).

Dependances : pip install numpy opencv-python-headless requests
"""
import argparse, csv, json, math, os, sys
from concurrent.futures import ThreadPoolExecutor
import cv2, numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('slug')
ap.add_argument('-o', '--out', default=None, help='fichier image de sortie (defaut : <slug>.jpg)')
ap.add_argument('--galerie', default='https://ashrin45.github.io/1_scanlame/')
ap.add_argument('--local', action='store_true')
ap.add_argument('--patches', type=int, default=0, metavar='N')
ap.add_argument('--champs', action='store_true')
ap.add_argument('--fond', type=float, default=0.97, help='un patch dont plus de cette fraction est du fond blanc est saute')
a = ap.parse_args()

if a.local:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lames', a.slug)
    def read(path, binary=True):
        p = os.path.join(base, path)
        if not os.path.isfile(p): return None
        return open(p, 'rb').read() if binary else open(p, encoding='utf-8').read()
else:
    import requests
    sess = requests.Session()
    base = a.galerie.rstrip('/') + '/lames/' + a.slug + '/'
    def read(path, binary=True):
        r = sess.get(base + path, timeout=60)
        if r.status_code != 200: return None
        return r.content if binary else r.text

j = json.loads(read('lame.json', binary=False) or 'null')
if not j: sys.exit(f'lame « {a.slug} » introuvable')
W, H, T, maxlvl = j['width'], j['height'], j['tile'], j['maxlvl']
print(f'{j["title"]} : {W} x {H} px, objectif {j.get("objective")}, {j.get("um_per_px") or "non calibré"} µm/px')

img = np.full((H, W, 3), 255, np.uint8)
coords = [(tx, ty) for ty in range(math.ceil(H / T)) for tx in range(math.ceil(W / T))]
def fetch(c):
    tx, ty = c
    data = read(f'lame_files/{maxlvl}/{tx}_{ty}.jpg')
    if data is None: return c, None
    return c, cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
with ThreadPoolExecutor(8) as ex:
    for n, ((tx, ty), t) in enumerate(ex.map(fetch, coords), 1):
        if t is None: continue
        ox, oy = max(0, tx * T - 1), max(0, ty * T - 1)          # tuiles Deep Zoom : 1 px de recouvrement
        h, w = t.shape[:2]
        img[oy:min(H, oy + h), ox:min(W, ox + w)] = t[:min(H, oy + h) - oy, :min(W, ox + w) - ox]
        if n % 50 == 0: print(f'  {n}/{len(coords)} tuiles', end='\r')
print(f'  {len(coords)} tuiles assemblées      ')
out = a.out or f'{a.slug}.jpg'
cv2.imwrite(out, img, [cv2.IMWRITE_JPEG_QUALITY, 95])
print(f'image écrite : {out} ({os.path.getsize(out) >> 20} Mo)')

if a.patches:
    N = a.patches; d = f'{a.slug}_patches'; os.makedirs(d, exist_ok=True); kept = 0
    with open(os.path.join(d, 'patches.csv'), 'w', newline='') as f:
        wr = csv.writer(f); wr.writerow(['fichier', 'x', 'y', 'taille', 'fond'])
        for y in range(0, H - N + 1, N):
            for x in range(0, W - N + 1, N):
                p = img[y:y + N, x:x + N]
                bg = float((p.min(axis=2) > 235).mean())
                if bg > a.fond: continue
                name = f'{a.slug}_{x:06d}_{y:06d}.jpg'
                cv2.imwrite(os.path.join(d, name), p, [cv2.IMWRITE_JPEG_QUALITY, 95]); wr.writerow([name, x, y, N, round(bg, 3)]); kept += 1
    print(f'{kept} patches de {N} px dans {d}/ (patches.csv : coordonnées)')

if a.champs:
    fields = json.loads(read('fields.json', binary=False) or '[]'); d = f'{a.slug}_champs'; os.makedirs(d, exist_ok=True)
    for fd in fields:
        pts = np.array(fd['pts']); x0, y0 = pts.min(0).clip(0); x1, y1 = np.minimum(pts.max(0), [W, H])
        if x1 - x0 < 32 or y1 - y0 < 32: continue
        cv2.imwrite(os.path.join(d, f'{fd["id"]}.jpg'), img[int(y0):int(y1), int(x0):int(x1)], [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(f'{len(fields)} champs dans {d}/')
