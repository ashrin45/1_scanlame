#!/usr/bin/env python3
"""Publie une lame assemblee sous forme de pyramide de tuiles + visionneuse dans lames/<nom>/,
puis regenere la galerie lames/index.html. Un `git push` suffit ensuite (GitHub Pages).

Usage : python tools/publish_lame.py pano.jpg --name gram-2026-10-14 --title "Gram, prelevement vaginal"
        [--fields layout.json] [--tile 512] [--quality 85]
"""
import argparse, json, math, os, re, html
import cv2

ap = argparse.ArgumentParser()
ap.add_argument('pano')
ap.add_argument('--name', required=True, help='slug du dossier (lettres, chiffres, tirets)')
ap.add_argument('--title', default=None)
ap.add_argument('--fields', default=None, help='fields.json : contours des champs [{id, pts:[[x,y],...]}]')
ap.add_argument('--tile', type=int, default=512)
ap.add_argument('--quality', type=int, default=85)
ap.add_argument('--note', default=None)
ap.add_argument('--um-per-px', type=float, default=None, help='micromètres par pixel de la mosaïque (barre d\'échelle)')
a = ap.parse_args()

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
slug = re.sub(r'[^a-z0-9-]+', '-', a.name.lower()).strip('-')
out = os.path.join(root, 'lames', slug)
tiles = os.path.join(out, 'lame_files')
os.makedirs(tiles, exist_ok=True)

img = cv2.imread(a.pano)
H, W = img.shape[:2]
maxlvl = math.ceil(math.log2(max(W, H)))
n = 0
for lvl in range(maxlvl, -1, -1):
    lh, lw = img.shape[:2]
    d = os.path.join(tiles, str(lvl)); os.makedirs(d, exist_ok=True)
    for ty in range(math.ceil(lh / a.tile)):
        for tx in range(math.ceil(lw / a.tile)):
            x0, y0 = max(0, tx * a.tile - 1), max(0, ty * a.tile - 1)
            x1, y1 = min(lw, (tx + 1) * a.tile + 1), min(lh, (ty + 1) * a.tile + 1)
            cv2.imwrite(os.path.join(d, f'{tx}_{ty}.jpg'), img[y0:y1, x0:x1], [cv2.IMWRITE_JPEG_QUALITY, a.quality]); n += 1
    if lvl == 0: break
    img = cv2.resize(img, (max(1, math.ceil(lw / 2)), max(1, math.ceil(lh / 2))), interpolation=cv2.INTER_AREA)
open(os.path.join(out, 'lame.dzi'), 'w').write(
    f'<?xml version="1.0" encoding="UTF-8"?>\n<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" Format="jpg" Overlap="1" TileSize="{a.tile}"><Size Width="{W}" Height="{H}"/></Image>')

fields = json.load(open(a.fields)) if a.fields else []
title = a.title or slug
note = a.note or (f'{a.um_per_px:.4g} µm/px' if a.um_per_px else 'échelle non calibrée')
meta = (f'<span><b>{len(fields)}</b> champs</span>' if fields else '') + f'<span><b>{W} × {H}</b> px</span><span><b>{maxlvl + 1}</b> niveaux de zoom</span><span>{html.escape(note)}</span>'
tpl = open(os.path.join(root, 'tools', 'viewer_template.html'), encoding='utf-8').read()
page = (tpl.replace('__TITLE__', html.escape(title)).replace('__META__', meta).replace('__W__', str(W)).replace('__H__', str(H))
        .replace('__FIELDS__', json.dumps(fields, separators=(',', ':'))).replace('__TILE__', str(a.tile)).replace('__UMPX__', json.dumps(a.um_per_px)))
open(os.path.join(out, 'index.html'), 'w', encoding='utf-8').write(page)
json.dump({'title': title, 'width': W, 'height': H, 'fields': len(fields), 'tiles': n, 'um_per_px': a.um_per_px}, open(os.path.join(out, 'lame.json'), 'w'))

# gallery
items = []
for d in sorted(os.listdir(os.path.join(root, 'lames'))):
    p = os.path.join(root, 'lames', d, 'lame.json')
    if os.path.isfile(p):
        j = json.load(open(p))
        items.append(f'<a class="card" href="{d}/"><img src="{d}/lame_files/{max(0, math.ceil(math.log2(max(j["width"], j["height"]))) - 4)}/0_0.jpg" alt=""><span><b>{html.escape(j["title"])}</b><small>{j["width"]} × {j["height"]} px</small></span></a>')
gallery = f'''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lames virtuelles</title>
<style>
:root {{ --bg:#f3f0f4; --panel:#fff; --fg:#2b2433; --muted:#6e6478; --line:#d9d2de; --accent:#7a2e8c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#17131b; --panel:#211c26; --fg:#ece6f0; --muted:#a79cb1; --line:#3a323f; --accent:#c58ad6; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.4 system-ui,sans-serif; padding:20px max(16px, env(safe-area-inset-right)) 40px max(16px, env(safe-area-inset-left)); }}
h1 {{ font-size:20px; margin:0 0 4px; }} p {{ color:var(--muted); margin:0 0 20px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:14px; }}
.card {{ display:block; background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow:hidden; color:inherit; text-decoration:none; }}
.card img {{ display:block; width:100%; aspect-ratio:3/2; object-fit:cover; background:#000; }}
.card span {{ display:flex; flex-direction:column; gap:2px; padding:10px 12px; }} .card small {{ color:var(--muted); }}
.card:hover {{ border-color:var(--accent); }}
</style></head><body>
<h1>Lames virtuelles</h1><p>Mosaïques assemblées à partir de photos prises à l'oculaire. Zoom au pincement, mini-carte en haut à droite.</p>
<div class="grid">{''.join(items)}</div>
</body></html>
'''
open(os.path.join(root, 'lames', 'index.html'), 'w', encoding='utf-8').write(gallery)
print(f'{slug}: {W}x{H}, {n} tuiles, {len(items)} lame(s) dans la galerie')
