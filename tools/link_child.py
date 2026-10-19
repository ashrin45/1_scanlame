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

def reg_image(slug, j, max_side=3000, full=False):
    """Image reduite de la lame (pano_reg.jpg, sinon reconstruite depuis les tuiles) + son echelle par rapport a la pleine resolution.
    full=True : pleine resolution reconstruite depuis les tuiles."""
    p = os.path.join(LAMES, slug, 'pano_reg.jpg')
    if full: max_side = 10 ** 9
    if os.path.isfile(p) and not full:
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

def first_field_center(slug):
    """Centre (px pleine resolution) de la premiere photo de la serie dans la mosaique : c'est la ou l'utilisateur etait au depart
    (la ou il a touche le repere, et la ou il a releve la platine)."""
    try: f = [x for x in json.load(open(os.path.join(LAMES, slug, 'fields.json'))) if x.get('pts')]
    except Exception: return None
    if not f: return None
    return np.array(min(f, key=lambda e: e['id'])['pts'], float).mean(axis=0)

def fit_stage(pj, origin_px):
    """Calibration platine du parent : delta mm (platine) -> delta px (parent), a partir des observations accumulees.
    1 observation : axes supposes alignes ou permutes avec la camera, on garde la variante la plus isotrope ;
    2 et plus : moindres carres sur une matrice 2x2."""
    obs = pj.get('stage_obs') or []
    M = np.array([o['mm'] for o in obs], float).reshape(-1, 2); P = np.array([o['px'] for o in obs], float).reshape(-1, 2) - origin_px
    if len(obs) == 0: return None
    if len(obs) >= 2 and np.linalg.matrix_rank(M, tol=0.05) == 2:
        A, *_ = np.linalg.lstsq(M, P, rcond=None); return A.T.tolist()
    dm, dp = M[0], P[0]
    best = None
    for perm in ((0, 1), (1, 0)):
        k = []
        for i in range(2):
            k.append(dp[i] / dm[perm[i]] if abs(dm[perm[i]]) > 0.05 else None)
        if all(v is None for v in k): continue
        mag = np.mean([abs(v) for v in k if v is not None])
        k = [v if v is not None else mag for v in k]
        iso = abs(math.log(abs(k[0]) / abs(k[1]))) if k[0] and k[1] else 9
        if best is None or iso < best[0]: best = (iso, perm, k)
    if not best: return None
    _, perm, k = best
    A = np.zeros((2, 2)); A[0, perm[0]] = k[0]; A[1, perm[1]] = k[1]
    return A.tolist()

def prior_center(pj, cj):
    """Position attendue (px parent) de la premiere photo enfant : coordonnees de platine si le parent est calibre, sinon repere touche."""
    ps, cs, cal = pj.get('stage'), cj.get('stage'), pj.get('stage_cal')
    if ps and cs and cal:
        o = first_field_center(pj['_slug'])
        if o is not None:
            d = np.array([cs['x'] - ps['x'], cs['y'] - ps['y']], float)
            return o + np.array(cal, float) @ d, 'platine'
    h = cj.get('hint')
    if h and h.get('fx') is not None: return np.array([h['fx'] * pj['width'], h['fy'] * pj['height']], float), 'repère'
    return None, None

def outline(slug, j, cim, sc):
    """Contour(s) de la zone couverte par la mosaique (px pleine resolution) : le fond blanc hors couverture est rogne a l'affichage."""
    bg = (cim.min(axis=2) > 245).astype(np.uint8)
    k = np.ones((7, 7), np.uint8)
    fg = cv2.morphologyEx(1 - bg, cv2.MORPH_CLOSE, k); fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k)
    cs, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    area = fg.sum()
    polys = []
    for c in sorted(cs, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(c) < 0.02 * area: break
        c = cv2.approxPolyDP(c, 3.0, True).reshape(-1, 2)
        polys.append([[round(float(x) / sc, 1), round(float(y) / sc, 1)] for x, y in c])
    return polys

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
    pj['_slug'] = parent
    prior, prior_kind = prior_center(pj, cj)
    sift = cv2.SIFT_create(8000); bf = cv2.BFMatcher(cv2.NORM_L2)
    gp = cv2.createCLAHE(2.0, (8, 8)).apply(cv2.cvtColor(pim, cv2.COLOR_BGR2GRAY))
    # avec un a priori, on cherche d'abord dans une fenetre autour (taille enfant attendue x 2, au moins 15 % du parent) : moins d'ambiguite
    windows = []
    if prior is not None:
        rr0 = r or 0.1
        hw = max(cj['width'] * rr0, 0.15 * pj['width']) * sp; hh = max(cj['height'] * rr0, 0.15 * pj['height']) * sp
        cx, cy = prior[0] * sp, prior[1] * sp
        x0, y0 = int(max(0, cx - hw)), int(max(0, cy - hh)); x1, y1 = int(min(gp.shape[1], cx + hw)), int(min(gp.shape[0], cy + hh))
        if x1 - x0 > 50 and y1 - y0 > 50: windows.append((x0, y0, x1, y1))
        print(f'a priori ({prior_kind}) : première photo attendue vers ({prior[0]:.0f}, {prior[1]:.0f}) px du parent')
    windows.append(None)
    best = None
    for win in windows:
      if best: break
      if win: gw = gp[win[1]:win[3], win[0]:win[2]]; print(f'recherche dans la fenêtre {win}')
      else: gw = gp
      kp, dp = sift.detectAndCompute(gw, None)
      if win and kp:
          for k_ in kp: k_.pt = (k_.pt[0] + win[0], k_.pt[1] + win[1])
      tried = set(); queue = list(cands)
      while queue:
          rr = queue.pop(0)
          if any(abs(rr / t - 1) < 0.05 for t in tried): continue
          tried.add(rr)
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
          # rapport confirme par une 2e estimation independante (echelle residuelle ~1 apres correction) : 15 inliers suffisent
          ok = (n >= 25 and 0.7 < s_ < 1.4) or (n >= 15 and 0.93 < s_ < 1.07 and rr not in cands)
          if ok and (best is None or n > best[0]): best = (n, A, f)
          # echelle residuelle loin de 1 : le rapport reel n'est pas celui attendu (zoom ou objectif different), on reessaie au rapport mesure
          elif n >= 10 and not (0.7 < s_ < 1.4) and 0.02 < rr * s_ < 1.5: queue.insert(0, rr * s_); print(f'  -> rapport réel probable {rr * s_:.3f}, nouvel essai')
    if best:
        n, A, f = best
        k = sc * f / sp
        transform = [float(A[0, 0] * k), float(A[1, 0] * k), float(A[0, 2] / sp), float(A[1, 2] / sp)]   # enfant pleine res -> parent pleine res
        print(f'recalage réussi : {n} inliers, échelle {math.hypot(transform[0], transform[1]):.4f}, rotation {math.degrees(math.atan2(transform[1], transform[0])):.1f}°')
        cj['parent_approx'] = None
        # calibration platine du parent : on memorise ou est tombee la 1re photo enfant pour ce delta de platine
        ps, cs = pj.get('stage'), cj.get('stage')
        c0 = first_field_center(child); o = first_field_center(parent)
        if ps and cs and c0 is not None and o is not None:
            t = transform; px = [t[0] * c0[0] - t[1] * c0[1] + t[2], t[1] * c0[0] + t[0] * c0[1] + t[3]]
            obs = [x for x in (pj.get('stage_obs') or []) if x.get('slug') != child] + [dict(slug=child, mm=[cs['x'] - ps['x'], cs['y'] - ps['y']], px=px)]
            pj['stage_obs'] = obs; pj['stage_cal'] = fit_stage(pj, o)
            if pj['stage_cal']: print(f'calibration platine du parent mise à jour ({len(obs)} observation(s)) : {np.round(pj["stage_cal"], 1).tolist()} px/mm')
    elif prior is not None:
        rr = r or 0.1; c0 = first_field_center(child)
        if c0 is None: c0 = np.array([cj['width'] / 2, cj['height'] / 2])
        transform = [rr, 0.0, float(prior[0] - rr * c0[0]), float(prior[1] - rr * c0[1])]
        cj['parent_approx'] = prior_kind
        print(f'recalage par points impossible : placement approché d\'après le {prior_kind} (échelle {rr:.3f})')
    else:
        transform = None
        print('recalage impossible : la lame de détail est seulement listée dans la lame d\'ensemble')
    pj.pop('_slug', None)
    cj['parent'] = parent; cj['parent_transform'] = transform; cj['outline'] = outline(child, cj, cim, sc) if transform else None; save_lame(child, cj)
    kids = [c for c in (pj.get('children') or []) if c['slug'] != child] + [dict(slug=child, transform=transform)]
    pj['children'] = kids; save_lame(parent, pj)
    write_pages(parent); write_pages(child); write_gallery()

if __name__ == '__main__':
    main()
