#!/usr/bin/env python3
"""Pipeline complet : serie (video ou photos + serie.json) -> mosaique -> pyramide de tuiles.

Usage : python tools/pipeline.py <dossier_serie> [--out <dossier>] [--fps 3] [--max-frames 250]
        [--reg-size 1200] [--max-side 12000] [--title "..."]

Etapes :
 1. images : extraction ffmpeg de la video (ou photos/*.jpg), rejet des floues, positions par horodatage
 2. pretraitement : masque du disque oculaire, correction de vignettage, balance des blancs
 3. recalage : SIFT + similarite RANSAC sur les paires voisines (voisinage temporel + positions estimees),
    placement glouton sur les meilleures paires, puis raffinement Gauss-Seidel
 4. composition : image dominante par region (nette, proche du centre), bord propre
 5. sortie : pano.jpg, layout.json, fields.json (pour publish_lame.py)
"""
import argparse, glob, itertools, json, math, os, subprocess, sys, time
import cv2, numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('series')
ap.add_argument('--out', default=None)
ap.add_argument('--fps', type=float, default=3.0)
ap.add_argument('--blur', type=float, default=25.0)
ap.add_argument('--max-frames', type=int, default=250)
ap.add_argument('--reg-size', type=int, default=1200, help='plus grand cote des images pour le recalage')
ap.add_argument('--max-side', type=int, default=12000, help='plus grand cote de la mosaique finale')
ap.add_argument('--min-inliers', type=int, default=40)
a = ap.parse_args()

S = a.series.rstrip('/')
out = a.out or os.path.join(S, 'result')
os.makedirs(out, exist_ok=True)
T0 = time.time()
def log(*m):
    msg = f'[{time.time() - T0:7.1f}s] ' + ' '.join(str(x) for x in m)
    print(msg, flush=True)
    open(os.path.join(out, 'pipeline.log'), 'a').write(msg + '\n')

meta = json.load(open(os.path.join(S, 'serie.json'))) if os.path.isfile(os.path.join(S, 'serie.json')) else {}
W0, H0 = meta.get('width'), meta.get('height')

# ---------------------------------------------------------------- 1. images
frames_dir = os.path.join(out, 'frames')
os.makedirs(frames_dir, exist_ok=True)
videos = [f for f in os.listdir(S) if f.endswith(('.mp4', '.webm'))]
photos = sorted(glob.glob(os.path.join(S, 'photos', '*.jpg'))) + sorted(glob.glob(os.path.join(S, '*.jpg')))
items = []   # (path, t_ms or None)
if videos and not glob.glob(os.path.join(frames_dir, 'f_*.jpg')):
    log('extraction', videos[0], 'à', a.fps, 'img/s')
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', os.path.join(S, videos[0]), '-vf', f'fps={a.fps}',
                    '-q:v', '2', '-frame_pts', '1', os.path.join(frames_dir, 'f_%08d.jpg')], check=True)
if videos:
    for p in sorted(glob.glob(os.path.join(frames_dir, 'f_*.jpg'))):
        items.append((p, int(os.path.basename(p)[2:-4]) * 1000.0 / a.fps))
else:
    byname = {f['name']: f for f in meta.get('frames', [])}
    for p in photos:
        f = byname.get(os.path.basename(p))
        items.append((p, f['t_ms'] if f else None))
if not items:
    sys.exit('aucune image')
log(len(items), 'images candidates')

# positions estimees (px du flux) par temps
samples = [f for f in meta.get('frames', []) if f.get('x') is not None and f.get('track') in ('ok', 'relocated')]
samples.sort(key=lambda f: f['t_ms'])
ts = np.array([f['t_ms'] for f in samples], float)
def pos_at(t_ms):
    if t_ms is None or not len(ts): return None
    k = int(np.argmin(np.abs(ts - t_ms)))
    return (samples[k]['x'], samples[k]['y']) if abs(ts[k] - t_ms) < 700 else None

# nettete + sous-echantillonnage spatial (on saute les images quasi identiques)
sel = []
last_pos = None
for p, t in items:
    g = cv2.imread(p, cv2.IMREAD_REDUCED_GRAYSCALE_4)
    sharp = float(cv2.Laplacian(g, cv2.CV_64F).var())
    if sharp < a.blur:
        continue
    pos = pos_at(t)
    if pos and last_pos and W0 and math.hypot(pos[0] - last_pos[0], pos[1] - last_pos[1]) < 0.12 * max(W0, H0):
        continue
    if pos: last_pos = pos
    sel.append(dict(path=p, t=t, sharp=sharp, pos=pos))
if len(sel) > a.max_frames:
    step = len(sel) / a.max_frames
    sel = [sel[int(i * step)] for i in range(a.max_frames)]
log(len(sel), 'images retenues (nettes, non redondantes)')
if len(sel) < 2:
    sys.exit('pas assez d\'images nettes')

# ---------------------------------------------------------------- 2. pretraitement
def preprocess(path, reg_size):
    im = cv2.imread(path)
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(g, 60, 255, cv2.THRESH_BINARY)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(th)
    if n > 1:
        big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        comp = (lab == big).astype(np.uint8) * 255
        cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        mask = np.zeros_like(comp); cv2.fillPoly(mask, [cv2.convexHull(max(cnts, key=cv2.contourArea))], 255)
    else:
        mask = np.full(g.shape, 255, np.uint8)
    frac = (mask > 0).mean()
    if frac < 0.97:   # disque oculaire visible : on recule du bord sombre
        er = max(10, int(0.03 * max(g.shape)))
        mask = cv2.erode(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (er, er)))
    ys, xs = np.where(mask > 0)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    crop, mask = im[y0:y1, x0:x1], mask[y0:y1, x0:x1]
    f32 = crop.astype(np.float32); m = (mask > 0).astype(np.float32)
    small = cv2.resize(f32 * m[..., None], None, fx=1 / 16, fy=1 / 16, interpolation=cv2.INTER_AREA)
    msmall = cv2.resize(m, None, fx=1 / 16, fy=1 / 16, interpolation=cv2.INTER_AREA)
    sig = max(crop.shape) * 0.12 / 16
    bgs = cv2.GaussianBlur(small, (0, 0), sig) / (cv2.GaussianBlur(msmall, (0, 0), sig)[..., None] + 1e-6)
    bg = cv2.resize(bgs, (crop.shape[1], crop.shape[0]), interpolation=cv2.INTER_LINEAR)
    ref = np.array([np.percentile(bg[..., c][mask > 0], 90) for c in range(3)])
    corr = np.clip(f32 * (ref / (bg + 1e-6)), 0, 255)
    wb = np.array([np.percentile(corr[..., c][mask > 0], 95) for c in range(3)])
    corr = np.clip(corr * (238.0 / np.maximum(wb, 1)), 0, 255).astype(np.uint8)
    corr[mask == 0] = 0
    s = min(1.0, reg_size / max(corr.shape[:2]))
    reg = cv2.resize(corr, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else corr
    rmask = cv2.resize(mask, (reg.shape[1], reg.shape[0]), interpolation=cv2.INTER_NEAREST)
    return corr, mask, reg, rmask, s, (x0, y0)

full_dir = os.path.join(out, 'full'); os.makedirs(full_dir, exist_ok=True)
regs, rmasks, scales, offsets = [], [], [], []
for i, it in enumerate(sel):
    corr, mask, reg, rmask, s, off = preprocess(it['path'], a.reg_size)
    cv2.imwrite(os.path.join(full_dir, f'{i:04d}.jpg'), corr, [cv2.IMWRITE_JPEG_QUALITY, 95])
    cv2.imwrite(os.path.join(full_dir, f'{i:04d}_mask.png'), mask)
    regs.append(reg); rmasks.append(rmask); scales.append(s); offsets.append(off)
    it['crop'] = [int(off[0]), int(off[1]), int(corr.shape[1]), int(corr.shape[0])]
    del corr, mask
N = len(regs)
log('prétraitement terminé,', N, 'images, recalage à', regs[0].shape[1], 'x', regs[0].shape[0])

# ---------------------------------------------------------------- 3. recalage
sift = cv2.SIFT_create(5000)
kps, des = [], []
for reg, m in zip(regs, rmasks):
    g = cv2.createCLAHE(2.0, (8, 8)).apply(cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY))
    k, d = sift.detectAndCompute(g, cv2.erode(m, np.ones((9, 9), np.uint8)))
    kps.append(k); des.append(d)
log('points SIFT :', int(np.mean([len(k) for k in kps])), 'par image en moyenne')

# paires candidates : voisinage temporel + recouvrement prevu par les positions
cand = set()
for i in range(N):
    for j in range(i + 1, min(N, i + 9)):
        cand.add((i, j))
P = [it['pos'] for it in sel]
if W0 and any(P):
    for i, j in itertools.combinations(range(N), 2):
        if P[i] and P[j]:
            dx, dy = abs(P[i][0] - P[j][0]), abs(P[i][1] - P[j][1])
            if dx < 0.75 * W0 and dy < 0.75 * H0:
                cand.add((i, j))
log(len(cand), 'paires à tester')

bf = cv2.BFMatcher(cv2.NORM_L2)
pairs = {}
for i, j in sorted(cand):
    if des[i] is None or des[j] is None or len(des[i]) < 20 or len(des[j]) < 20: continue
    mm = bf.knnMatch(des[i], des[j], k=2)
    good = [m for m, n in mm if m.distance < 0.75 * n.distance]
    if len(good) < 12: continue
    pi = np.float32([kps[i][m.queryIdx].pt for m in good]); pj = np.float32([kps[j][m.trainIdx].pt for m in good])
    A, inl = cv2.estimateAffinePartial2D(pj, pi, method=cv2.RANSAC, ransacReprojThreshold=4.0, maxIters=4000, confidence=0.999)
    if A is None: continue
    n_inl = int(inl.sum())
    sc = math.hypot(A[0, 0], A[0, 1])
    if n_inl >= a.min_inliers and 0.5 < sc < 2.0:
        s_ = inl.ravel() > 0
        pairs[(i, j)] = (pi[s_], pj[s_], A, n_inl)
log(len(pairs), 'paires valides')

adj = {i: {} for i in range(N)}
for (i, j), (_, _, _, n) in pairs.items(): adj[i][j] = n; adj[j][i] = n
seen, comps = set(), []
for s0 in range(N):
    if s0 in seen: continue
    comp, stack = [], [s0]
    while stack:
        u = stack.pop()
        if u in seen: continue
        seen.add(u); comp.append(u); stack.extend(adj[u])
    comps.append(comp)
comps.sort(key=len, reverse=True)
comp = comps[0]
log('composantes :', [len(c) for c in comps], '-> on garde', len(comp), 'images')
if len(comp) < 2:
    sys.exit('aucune paire d\'images ne se recouvre')

def A3(A): return np.vstack([A, [0, 0, 1]])
ref = max(comp, key=lambda k: sum(adj[k].values()))
T = {ref: np.eye(3)}
while len(T) < len(comp):
    best = None
    for k in T:
        for v, n in adj[k].items():
            if v not in T and (best is None or n > best[2]): best = (k, v, n)
    k, v, _ = best
    T[v] = T[k] @ (A3(pairs[(k, v)][2]) if (k, v) in pairs else np.linalg.inv(A3(pairs[(v, k)][2])))
for it in range(10):
    for v in comp:
        if v == ref: continue
        src, dst = [], []
        for (i, j), (pi, pj, _, _) in pairs.items():
            if j == v and i in T: src.append(pj); dst.append(cv2.transform(pi[None], T[i][:2])[0])
            elif i == v and j in T: src.append(pi); dst.append(cv2.transform(pj[None], T[j][:2])[0])
        if not src: continue
        A, _ = cv2.estimateAffinePartial2D(np.vstack(src), np.vstack(dst), method=cv2.LMEDS)
        if A is not None: T[v] = A3(A)
# erreur residuelle
res = []
for (i, j), (pi, pj, _, _) in pairs.items():
    if i in T and j in T:
        d = cv2.transform(pi[None], T[i][:2])[0] - cv2.transform(pj[None], T[j][:2])[0]
        res.append(np.median(np.hypot(d[:, 0], d[:, 1])))
log(f'recalage : erreur médiane {np.median(res):.2f} px (échelle recalage), max {np.max(res):.2f}')

# ---------------------------------------------------------------- 4. composition
# transformation pleine resolution : T est exprime en pixels de l'image de recalage ; on remonte a l'image pleine
Tf = {}
for k in comp:
    s = scales[k]
    Tf[k] = T[k] @ np.diag([s, s, 1])          # pleine res -> ref (ref en px recalage)
sc_ref = 1.0 / scales[ref]                      # pour que l'image de reference soit a l'echelle 1
smin = min(math.hypot((Tf[k] @ np.diag([1, 1, 1]))[0, 0], Tf[k][0, 1]) for k in comp)
Sref = np.diag([1 / smin, 1 / smin, 1])          # l'image la plus zoomee reste a l'echelle 1
corners = []
for k in comp:
    h, w = cv2.imread(os.path.join(full_dir, f'{k:04d}_mask.png'), 0).shape
    corners.append(cv2.transform(np.float32([[0, 0], [w, 0], [w, h], [0, h]])[None], (Sref @ Tf[k])[:2])[0])
corners = np.vstack(corners); mn, mx = corners.min(0), corners.max(0)
W, H = mx - mn
shrink = min(1.0, a.max_side / max(W, H))
if shrink < 1: log(f'mosaïque réduite d\'un facteur {shrink:.2f} pour tenir dans {a.max_side} px')
Sfin = np.diag([shrink, shrink, 1]) @ np.array([[1, 0, -mn[0]], [0, 1, -mn[1]], [0, 0, 1]]) @ Sref
W, H = int(math.ceil(W * shrink)), int(math.ceil(H * shrink))
log('mosaïque', W, 'x', H)
acc = np.zeros((H, W, 3), np.float32); wacc = np.zeros((H, W), np.float32); cov = np.zeros((H, W), np.uint8)
placements, fields = {}, []
for k in comp:
    M = (Sfin @ Tf[k])[:2]
    im = cv2.imread(os.path.join(full_dir, f'{k:04d}.jpg')); mk = cv2.imread(os.path.join(full_dir, f'{k:04d}_mask.png'), 0)
    s = math.hypot(M[0, 0], M[0, 1])
    warped = cv2.warpAffine(im, M, (W, H), flags=cv2.INTER_LINEAR)
    wm = cv2.warpAffine(mk, M, (W, H), flags=cv2.INTER_NEAREST)
    dist = cv2.distanceTransform((wm > 0).astype(np.uint8), cv2.DIST_L2, 5)
    wgt = (dist / max(float(dist.max()), 1.0)) ** 6 / (s ** 4)
    acc += warped.astype(np.float32) * wgt[..., None]; wacc += wgt
    cov = np.maximum(cov, cv2.erode(wm, np.ones((9, 9), np.uint8)))
    h, w = mk.shape
    pts = cv2.transform(np.float32([[0, 0], [w, 0], [w, h], [0, h]])[None], M)[0]
    fields.append(dict(id=f'{k:04d}', pts=[[int(round(x)), int(round(y))] for x, y in pts], scale=round(1 / s, 3)))
    placements[k] = M.tolist()
    del warped, wm, dist, wgt, im, mk
pano = np.clip(np.divide(acc, wacc[..., None], out=np.full_like(acc, 255.0), where=wacc[..., None] > 0), 0, 255).astype(np.uint8)
pano[cov == 0] = 255
del acc, wacc
cv2.imwrite(os.path.join(out, 'pano.jpg'), pano, [cv2.IMWRITE_JPEG_QUALITY, 90])
json.dump(fields, open(os.path.join(out, 'fields.json'), 'w'))
json.dump(dict(size=[W, H], images=len(comp), of=N, placements={str(k): v for k, v in placements.items()},
               residual_px=float(np.median(res)), series=meta.get('series'), sources=[sel[k]['path'] for k in comp]),
          open(os.path.join(out, 'layout.json'), 'w'), indent=1)
log('terminé : pano.jpg', W, 'x', H, ',', len(comp), 'images utilisées sur', N)
