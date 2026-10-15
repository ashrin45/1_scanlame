#!/usr/bin/env python3
"""Reprise ciblee : fond une nouvelle prise (assemblee seule par pipeline.py) dans une lame deja publiee.
La nouvelle mosaique est recalee par points SIFT dans l'ancienne, puis ne remplit que ce que l'ancienne ne
couvrait pas (trous, bords), avec un fondu sur la frontiere. Les contours de champs et le rapport de qualite
sont mis a jour. Ecrit pano.jpg, fields.json, layout.json dans <result>/merged/ pour publish_lame.py.

Usage : python tools/merge_retake.py <slug_lame> <dossier_result_de_la_reprise>
"""
import json, math, os, sys
import cv2, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from publish_lame import LAMES, load_lame
from link_child import reg_image

def coverage(im):
    """Zones composees : le fond non couvert est blanc pur (255), l'interieur des champs ne l'est jamais tout a fait."""
    white = np.all(im >= 251, axis=2).astype(np.uint8)
    white = cv2.morphologyEx(white, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    return (1 - white).astype(np.uint8)

def main():
    slug, res = sys.argv[1], sys.argv[2]
    j = load_lame(slug)
    if not j: sys.exit('lame introuvable')
    old, so = reg_image(slug, j, full=True)                     # pleine resolution depuis les tuiles
    assert abs(so - 1) < 1e-6
    oreg, sp = reg_image(slug, j)                               # version reduite pour le recalage
    new = cv2.imread(os.path.join(res, 'pano.jpg'))
    nfields = json.load(open(os.path.join(res, 'fields.json')))
    nlayout = json.load(open(os.path.join(res, 'layout.json')))
    sn = min(1.0, 3000 / max(new.shape[:2]))
    nreg = cv2.resize(new, None, fx=sn, fy=sn, interpolation=cv2.INTER_AREA) if sn < 1 else new
    # --- recalage de la reprise dans l'ancienne mosaique (meme objectif attendu : echelle proche de 1) ---
    sift = cv2.SIFT_create(12000); bf = cv2.BFMatcher(cv2.NORM_L2)
    cl = cv2.createCLAHE(2.0, (8, 8))
    kp, dp = sift.detectAndCompute(cl.apply(cv2.cvtColor(oreg, cv2.COLOR_BGR2GRAY)), coverage(oreg) * 255)
    kn, dn = sift.detectAndCompute(cl.apply(cv2.cvtColor(nreg, cv2.COLOR_BGR2GRAY)), coverage(nreg) * 255)
    if dp is None or dn is None: sys.exit('pas assez de points')
    good = [m for m, n in bf.knnMatch(dn, dp, k=2) if m.distance < 0.75 * n.distance]
    if len(good) < 15: sys.exit(f'recalage impossible ({len(good)} appariements) : la reprise ne recouvre pas assez l\'ancienne lame')
    src = np.float32([kn[m.queryIdx].pt for m in good]); dst = np.float32([kp[m.trainIdx].pt for m in good])
    A, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0, maxIters=6000, confidence=0.999)
    n = int(inl.sum()) if inl is not None else 0
    s_ = math.hypot(A[0, 0], A[0, 1]) * sn / sp if A is not None else 0
    print(f'recalage : {len(good)} appariements, {n} inliers, échelle {s_:.3f}')
    if A is None or n < 30 or not (0.5 < s_ < 2.0): sys.exit('recalage impossible : trop peu de points communs avec l\'ancienne lame')
    # new pleine res -> old pleine res
    T = np.diag([1 / sp, 1 / sp, 1]) @ np.vstack([A, [0, 0, 1]]) @ np.diag([sn, sn, 1])
    # --- canevas commun ---
    nh, nw = new.shape[:2]; oh, ow = old.shape[:2]
    c4 = cv2.transform(np.float32([[0, 0], [nw, 0], [nw, nh], [0, nh]])[None], T[:2])[0]
    mn = np.minimum(c4.min(0), [0, 0]); mx = np.maximum(c4.max(0), [ow, oh])
    offx, offy = int(math.floor(mn[0])), int(math.floor(mn[1]))
    Wm, Hm = int(math.ceil(mx[0])) - offx, int(math.ceil(mx[1])) - offy
    print(f'canevas : {ow} x {oh} -> {Wm} x {Hm}')
    merged = np.full((Hm, Wm, 3), 255, np.uint8)
    merged[-offy:-offy + oh, -offx:-offx + ow] = old
    ocov = np.zeros((Hm, Wm), np.uint8); ocov[-offy:-offy + oh, -offx:-offx + ow] = coverage(old)
    del old
    Tm = T.copy(); Tm[0, 2] -= offx; Tm[1, 2] -= offy
    nw_ = cv2.warpAffine(new, Tm[:2], (Wm, Hm), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    ncov = cv2.warpAffine(coverage(new), Tm[:2], (Wm, Hm), flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    ncov = cv2.erode(ncov, np.ones((7, 7), np.uint8))           # on reste a l'ecart du bord de la reprise
    del new
    # --- fusion : l'ancienne garde la main la ou elle couvre, fondu sur 60 px vers la reprise a sa frontiere ---
    fill = (ncov > 0) & (ocov == 0)
    both = (ncov > 0) & (ocov > 0)
    ys, xs = np.where(ncov > 0)
    if len(ys):
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        dist = cv2.distanceTransform(ocov[y0:y1, x0:x1], cv2.DIST_L2, 5)
        alpha = np.clip(dist / 60.0, 0, 1).astype(np.float32)   # 1 loin dans l'ancienne, 0 a sa frontiere
        b = both[y0:y1, x0:x1]
        reg_m = merged[y0:y1, x0:x1]; reg_n = nw_[y0:y1, x0:x1]
        mix = (reg_m.astype(np.float32) * alpha[..., None] + reg_n.astype(np.float32) * (1 - alpha[..., None]))
        reg_m[b] = np.clip(mix, 0, 255).astype(np.uint8)[b]
        reg_m[fill[y0:y1, x0:x1]] = reg_n[fill[y0:y1, x0:x1]]
    added = int(fill.sum())
    print(f'surface ajoutée : {added} px ({100 * added / max(1, ocov.sum()):.1f} % de l\'ancienne couverture)')
    out = os.path.join(res, 'merged'); os.makedirs(out, exist_ok=True)
    cv2.imwrite(os.path.join(out, 'pano.jpg'), merged, [cv2.IMWRITE_JPEG_QUALITY, 92])
    cov = ((ocov > 0) | (ncov > 0)).astype(np.uint8)
    del merged, nw_
    # --- champs : anciens decales + nouveaux transformes ---
    ofields = json.load(open(os.path.join(LAMES, slug, 'fields.json'))) if os.path.isfile(os.path.join(LAMES, slug, 'fields.json')) else []
    k = (j.get('retakes') or 0) + 1
    fields = [dict(f, pts=[[x - offx, y - offy] for x, y in f['pts']]) for f in ofields]
    for f in nfields:
        pts = cv2.transform(np.float32(f['pts'])[None], Tm[:2])[0]
        fields.append(dict(f, id=f'r{k}-{f["id"]}', pts=[[int(round(x)), int(round(y))] for x, y in pts]))
    json.dump(fields, open(os.path.join(out, 'fields.json'), 'w'), separators=(',', ':'))
    # --- qualite : trous recalcules sur la couverture fusionnee, le reste herite ---
    SQ = 8
    cov_s = cv2.resize(cov * 255, (max(1, Wm // SQ), max(1, Hm // SQ)), interpolation=cv2.INTER_AREA) > 127
    pad = np.pad(~cov_s, 1, constant_values=True).astype(np.uint8)
    n_, lab_, st_, _ = cv2.connectedComponentsWithStats(pad, connectivity=4)
    outside = set(np.unique(np.concatenate([lab_[0], lab_[-1], lab_[:, 0], lab_[:, -1]])))
    field_area = float(np.median([cv2.contourArea(np.float32(f['pts'])) for f in fields])) / (SQ * SQ) if fields else 1e9
    holes, gap_px = [], 0
    for c in range(1, n_):
        if c in outside or st_[c, cv2.CC_STAT_AREA] < max(4, 0.01 * field_area): continue
        gap_px += int(st_[c, cv2.CC_STAT_AREA])
        cnts, _ = cv2.findContours((lab_ == c).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cn in cnts:
            cn = cv2.approxPolyDP(cn, 1.5, True); holes.append([[int((x - 1) * SQ), int((y - 1) * SQ)] for x, y in cn[:, 0, :]])
    holes_frac = gap_px / max(1, cov_s.sum())
    oq = j.get('quality') or {}; nq = nlayout.get('quality') or {}
    low_frac = float(np.mean([f.get('sharp', 1) < 0.5 for f in fields])) if fields else 0
    res_med = max(oq.get('residual_px') or 0, nq.get('residual_px') or 0)
    notes, score = [], 0
    if res_med > 3.0: notes.append(f'recalage imprécis ({res_med:.1f} px)'); score += 2
    elif res_med > 1.5: notes.append(f'recalage moyen ({res_med:.1f} px)'); score += 1
    if holes_frac > 0.08 or len(holes) >= 4: notes.append(f'{len(holes)} trou(s) dans la couverture ({holes_frac * 100:.0f} %)'); score += 2
    elif holes: notes.append(f'{len(holes)} trou(s) dans la couverture'); score += 1
    if low_frac > 0.25: notes.append(f'{low_frac * 100:.0f} % de champs peu nets'); score += 2
    elif low_frac > 0.10: notes.append(f'{low_frac * 100:.0f} % de champs peu nets'); score += 1
    tracked = sum(1 for f in fields if f.get('tracked'))
    if tracked: notes.append(f'{tracked} champ(s) placé(s) sans recalage fin'); score += 1
    notes.append(f'complétée par {k} reprise(s)')
    um = j.get('um_per_px')
    quality = dict(oq, grade='A' if score == 0 else 'B' if score <= 2 else 'C', notes=notes, holes=holes, holes_frac=round(holes_frac, 4),
                   coverage_px=int(cov.sum()), coverage_mm2=(round(int(cov.sum()) * um * um / 1e6, 3) if um else None),
                   low_sharp_frac=round(low_frac, 3), residual_px=round(res_med, 2), retakes=k)
    layout = dict(size=[Wm, Hm], images=len(fields), um_per_px=um, quality=quality, zstack=0,
                  title=j['title'], stain=j.get('stain'), objective=j.get('objective'), sample=j.get('sample'), comment=j.get('comment'),
                  parent=j.get('parent'), series=nlayout.get('series'), retakes=k, retake_series=(j.get('retake_series') or []) + [nlayout.get('series')])
    json.dump(layout, open(os.path.join(out, 'layout.json'), 'w'), indent=1)
    print(f'reprise fusionnée : {len(nfields)} champs ajoutés, {len(holes)} trou(s) restant(s), note {quality["grade"]}')

if __name__ == '__main__':
    main()
