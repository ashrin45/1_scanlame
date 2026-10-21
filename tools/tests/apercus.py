#!/usr/bin/env python3
"""Apercus des assemblages joues par test_pipeline.py, pour la comparaison visuelle de deux variantes.

  python tools/tests/apercus.py raw/_tests apercus/

Par serie : <serie>_ensemble.jpg et <serie>_detailN.jpg (mosaiques reduites a 2400 px), et <serie>_detailN_dans_ensemble.jpg
(la mosaique du detail posee dans l'ensemble avec la transformation trouvee, a 50 % d'opacite, contour en vert).
"""
import json, os, sys
import cv2, numpy as np

src, dst = sys.argv[1], sys.argv[2]; os.makedirs(dst, exist_ok=True)
MAXS = 2400

def load(p):
    im = cv2.imread(p, cv2.IMREAD_REDUCED_COLOR_2)
    if im is None: return None, 1
    f = 2.0
    s = MAXS / max(im.shape[:2])
    if s < 1: im = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA); f /= s
    return im, f                     # f : px du pano par px de l'apercu

for name in sorted(os.listdir(src)):
    d = os.path.join(src, name)
    ens, fe = load(os.path.join(d, 'ensemble', 'pano.jpg'))
    if ens is None: continue
    cv2.imwrite(os.path.join(dst, f'{name}_ensemble.jpg'), ens, [cv2.IMWRITE_JPEG_QUALITY, 85])
    for part in sorted(os.listdir(d)):
        if not part.startswith('detail'): continue
        det, fd = load(os.path.join(d, part, 'pano.jpg'))
        if det is None: continue
        cv2.imwrite(os.path.join(dst, f'{name}_{part}.jpg'), det, [cv2.IMWRITE_JPEG_QUALITY, 85])
        try: pt = json.load(open(os.path.join(d, part, 'layout.json'))).get('parent_transform')
        except Exception: pt = None
        if not pt: continue
        a, b, tx, ty = pt
        M = np.array([[a, -b, tx], [b, a, ty], [0, 0, 1]])                       # px detail -> px ensemble
        S = np.diag([1 / fe, 1 / fe, 1]) @ M @ np.diag([fd, fd, 1])             # apercu detail -> apercu ensemble
        h, w = ens.shape[:2]
        warped = cv2.warpAffine(det, S[:2], (w, h), flags=cv2.INTER_AREA)
        mask = cv2.warpAffine((det.max(2) > 8).astype(np.uint8) * 255, S[:2], (w, h)) > 0
        out = ens.copy(); out[mask] = (0.5 * ens[mask] + 0.5 * warped[mask]).astype(np.uint8)
        cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, cnts, -1, (120, 214, 127), 2)
        cv2.imwrite(os.path.join(dst, f'{name}_{part}_dans_ensemble.jpg'), out, [cv2.IMWRITE_JPEG_QUALITY, 85])
        # zoom en damier a pleine resolution de l'ensemble : une case sur deux vient du detail ; un decalage casse les cellules aux bords des cases
        full = cv2.imread(os.path.join(d, 'ensemble', 'pano.jpg'))
        if full is None: continue
        S2 = M @ np.diag([fd, fd, 1])                                          # apercu detail -> px ensemble
        ys, xs = np.nonzero(mask); x0, x1 = int(xs.min() * fe) - 40, int(xs.max() * fe) + 40; y0, y1 = int(ys.min() * fe) - 40, int(ys.max() * fe) + 40
        x0, y0 = max(0, x0), max(0, y0); x1, y1 = min(full.shape[1], x1), min(full.shape[0], y1)
        T = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]]) @ S2
        crop = full[y0:y1, x0:x1].copy()
        wd = cv2.warpAffine(det, T[:2], (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR)
        md = cv2.warpAffine((det.max(2) > 8).astype(np.uint8), T[:2], (x1 - x0, y1 - y0)) > 0
        yy, xx = np.mgrid[0:y1 - y0, 0:x1 - x0]; chk = ((yy // 96 + xx // 96) % 2 == 0) & md
        crop[chk] = wd[chk]
        cv2.imwrite(os.path.join(dst, f'{name}_{part}_damier.jpg'), crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(name, 'ok')
