#!/usr/bin/env python3
"""Rattache une lame de detail (autre objectif, ex. x100) a sa lame d'ensemble (ex. x10) : la mosaique
enfant est recalee par points SIFT dans la mosaique parente, et la visionneuse parente l'affiche en calque
a sa place. Sans recalage possible, l'enfant reste liste dans la lame parente (lien seulement).

Usage : python tools/link_child.py <slug_parent> <slug_enfant>
"""
import json, math, os, re, sys
import cv2, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from publish_lame import LAMES, load_lame, save_lame, write_pages, write_gallery

def reg_image(slug, j, max_side=3000):
    """Image reduite de la lame (pano_reg.jpg, sinon reconstruite depuis les tuiles) + son echelle par rapport a la pleine resolution."""
    p = os.path.join(LAMES, slug, 'pano_reg.jpg')
    if os.path.isfile(p):
        im = cv2.imread(p); return im, im.shape[1] / j['width']
    W, H, T, maxlvl = j['width'], j['height'], j['tile'], j['maxlvl']
    lvl = maxlvl
    while max(W, H) / 2 ** (maxlvl - lvl) > max_side and lvl > 0: lvl -= 1
    f = 1 / 2 ** (maxlvl - lvl); lw, lh = math.ceil(W * f), math.ceil(H * f)
    im = np.full((lh, lw, 3), 238, np.uint8)
    for ty in range(math.ceil(lh / T)):
        for tx in range(math.ceil(lw / T)):
            t = cv2.imread(os.path.join(LAMES, slug, 'lame_files', str(lvl), f'{tx}_{ty}.jpg'))
            if t is None: continue
            x0, y0 = max(0, tx * T - 1), max(0, ty * T - 1)
            im[y0:y0 + t.shape[0], x0:x0 + t.shape[1]] = t[:lh - y0, :lw - x0]
    return im, f

def objective_number(o):
    m = re.search(r'(\d+(?:[.,]\d+)?)', str(o or ''))
    return float(m[1].replace(',', '.')) if m else None

def main():
    parent, child = sys.argv[1], sys.argv[2]
    pj, cj = load_lame(parent), load_lame(child)
    if not pj or not cj: sys.exit('lame introuvable')
    pim, sp = reg_image(parent, pj); cim, sc = reg_image(child, cj)
    # rapport attendu : px enfant -> px parent
    r = None
    if pj.get('um_per_px') and cj.get('um_per_px'): r = cj['um_per_px'] / pj['um_per_px']
    else:
        po, co = objective_number(pj.get('objective')), objective_number(cj.get('objective'))
        if po and co: r = po / co
    cands = [r] if r else [0.1, 0.16, 0.25, 0.4, 0.63, 1.0]
    sift = cv2.SIFT_create(8000); bf = cv2.BFMatcher(cv2.NORM_L2)
    gp = cv2.createCLAHE(2.0, (8, 8)).apply(cv2.cvtColor(pim, cv2.COLOR_BGR2GRAY))
    kp, dp = sift.detectAndCompute(gp, None)
    best = None
    for rr in cands:
        f = rr * sp / sc                       # facteur a appliquer a l'image enfant reduite pour tomber a l'echelle du parent reduit
        cs = cv2.resize(cim, None, fx=f, fy=f, interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC)
        gc = cv2.createCLAHE(2.0, (8, 8)).apply(cv2.cvtColor(cs, cv2.COLOR_BGR2GRAY))
        kc, dc = sift.detectAndCompute(gc, None)
        if dc is None or dp is None or len(dc) < 20: continue
        good = [m for m, n in bf.knnMatch(dc, dp, k=2) if m.distance < 0.75 * n.distance]
        if len(good) < 12: continue
        src = np.float32([kc[m.queryIdx].pt for m in good]); dst = np.float32([kp[m.trainIdx].pt for m in good])
        A, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=5.0, maxIters=5000, confidence=0.999)
        if A is None: continue
        n = int(inl.sum()); s_ = math.hypot(A[0, 0], A[0, 1])
        print(f'rapport {rr:.3f} : {len(good)} appariements, {n} inliers, échelle résiduelle {s_:.2f}')
        if n >= 25 and 0.7 < s_ < 1.4 and (best is None or n > best[0]): best = (n, A, f)
    if best:
        n, A, f = best
        k = sc * f / sp
        transform = [float(A[0, 0] * k), float(A[1, 0] * k), float(A[0, 2] / sp), float(A[1, 2] / sp)]   # enfant pleine res -> parent pleine res
        print(f'recalage réussi : {n} inliers, échelle {math.hypot(transform[0], transform[1]):.4f}, rotation {math.degrees(math.atan2(transform[1], transform[0])):.1f}°')
    else:
        transform = None
        print('recalage impossible : la lame de détail est seulement listée dans la lame d\'ensemble')
    cj['parent'] = parent; cj['parent_transform'] = transform; save_lame(child, cj)
    kids = [c for c in (pj.get('children') or []) if c['slug'] != child] + [dict(slug=child, transform=transform)]
    pj['children'] = kids; save_lame(parent, pj)
    write_pages(parent); write_pages(child); write_gallery()

if __name__ == '__main__':
    main()
