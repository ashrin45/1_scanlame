#!/usr/bin/env python3
"""Pipeline complet : serie (video ou photos + serie.json) -> mosaique -> pyramide de tuiles.

Usage : python tools/pipeline.py <dossier_serie> [--out <dossier>] [--fps 3] [--max-frames 250]
        [--reg-size 1200] [--max-side 12000] [--title "..."]

Etapes :
 1. images : extraction ffmpeg de la video (ou photos/*.jpg), rejet des floues, positions par horodatage
 2. pretraitement : masque du disque oculaire, flat-field (champ vide filme, sinon estime), balance des blancs,
    correction de l'aberration chromatique (recalage radial des canaux R et B sur G)
 3. recalage : SIFT + similarite RANSAC sur les paires voisines, estimation de la distorsion radiale (k1),
    placement glouton puis ajustement global par moindres carres (toutes les paires a la fois, Huber)
 4. composition par tuiles : coeur net de chaque champ en priorite, coutures sur l'image dominante,
    fondu multibande (pyramide laplacienne)
 5. sortie : pano.jpg, layout.json, fields.json (pour publish_lame.py)
"""
import argparse, glob, itertools, json, math, os, subprocess, sys, time
import cv2, numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('series')
ap.add_argument('--out', default=None)
ap.add_argument('--fps', type=float, default=4.0)
ap.add_argument('--blur', type=float, default=25.0)
ap.add_argument('--max-frames', type=int, default=600)
ap.add_argument('--reg-size', type=int, default=1200, help='plus grand cote des images pour le recalage')
ap.add_argument('--max-side', type=int, default=16000, help='plus grand cote de la mosaique finale')
ap.add_argument('--min-inliers', type=int, default=40)
ap.add_argument('--core', type=float, default=0.70, help='fraction du rayon de champ utilisee en priorite (coeur net)')
ap.add_argument('--no-distortion', action='store_true', help='ne pas estimer la distorsion radiale')
ap.add_argument('--no-chroma', action='store_true', help='ne pas corriger l aberration chromatique')
ap.add_argument('--tile', type=int, default=2048, help='taille des tuiles de composition')
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
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-err_detect', 'ignore_err', '-fflags', '+genpts+discardcorrupt', '-i', os.path.join(S, videos[0]), '-vf', f'fps={a.fps}',
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
# ---- netteté et deplacement image a image (correlation de phase sur 256 px) ----
sharps, smalls = [], []
for p, t in items:
    g = cv2.imread(p, cv2.IMREAD_REDUCED_GRAYSCALE_4)
    sharps.append(float(cv2.Laplacian(g, cv2.CV_64F).var()))
    smalls.append(cv2.resize(g, (256, 256), interpolation=cv2.INTER_AREA).astype(np.float32))
h4, w4 = cv2.imread(items[0][0], cv2.IMREAD_REDUCED_GRAYSCALE_4).shape
win = cv2.createHanningWindow((256, 256), cv2.CV_32F)
moves = [0.0]
for i in range(1, len(smalls)):
    (dx, dy), resp = cv2.phaseCorrelate(smalls[i - 1], smalls[i], win)
    moves.append(math.hypot(dx / 256, dy / 256) if resp > 0.05 else 1.0)   # fraction du cadre ; pas de correlation = "a bouge"
del smalls

# ---- piles de profondeur : suites d'images immobiles dont la nettete varie (vis micrometrique tournee) ----
def edf(paths):
    """Fusion de plans de mise au point : en chaque pixel on garde le plan le plus net (pyramide laplacienne)."""
    imgs = [cv2.imread(q) for q in paths]
    ref = cv2.cvtColor(imgs[0], cv2.COLOR_BGR2GRAY).astype(np.float32)
    aligned, maps = [], []
    for im in imgs:
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32)
        (dx, dy), resp = cv2.phaseCorrelate(ref, g)
        if resp > 0.05 and abs(dx) < 60 and abs(dy) < 60:
            M = np.float32([[1, 0, -dx], [0, 1, -dy]]); im = cv2.warpAffine(im, M, (im.shape[1], im.shape[0]), borderMode=cv2.BORDER_REPLICATE); g = cv2.warpAffine(g, M, (g.shape[1], g.shape[0]), borderMode=cv2.BORDER_REPLICATE)
        aligned.append(im.astype(np.float32))
        maps.append(cv2.GaussianBlur(np.abs(cv2.Laplacian(g, cv2.CV_32F, ksize=3)), (0, 0), 6))
    M = np.stack(maps); M = (M / (M.max(0, keepdims=True) + 1e-6)) ** 8; M = M / (M.sum(0, keepdims=True) + 1e-9)
    levels = 5
    out_pyr = None
    for im, w in zip(aligned, M):
        gp = [im]; wp = [w]
        for _ in range(levels):
            gp.append(cv2.pyrDown(gp[-1])); wp.append(cv2.pyrDown(wp[-1]))
        lp = [gp[i] - cv2.pyrUp(gp[i + 1], dstsize=(gp[i].shape[1], gp[i].shape[0])) for i in range(levels)] + [gp[levels]]
        contrib = [l * wl[..., None] for l, wl in zip(lp, wp)]
        out_pyr = contrib if out_pyr is None else [o + c for o, c in zip(out_pyr, contrib)]
    res = out_pyr[levels]
    for i in range(levels - 1, -1, -1):
        res = cv2.pyrUp(res, dstsize=(out_pyr[i].shape[1], out_pyr[i].shape[0])) + out_pyr[i]
    return np.clip(res, 0, 255).astype(np.uint8)

STILL = 0.02   # deplacement < 2 % du cadre = immobile
merged, i, stacks = [], 0, 0
while i < len(items):
    j = i + 1
    while j < len(items) and moves[j] < STILL: j += 1
    run = list(range(i, j))
    if len(run) >= 3:
        sh = [sharps[k] for k in run]
        if max(sh) > 1.5 * min(sh) and max(sh) >= 3.0:
            outp = os.path.join(frames_dir, f'z_{i:06d}.jpg')
            cv2.imwrite(outp, edf([items[k][0] for k in run]), [cv2.IMWRITE_JPEG_QUALITY, 95])
            merged.append((outp, items[run[len(run) // 2]][1], max(sh))); stacks += 1
        else:
            k = max(run, key=lambda k: sharps[k]); merged.append((items[k][0], items[k][1], sharps[k]))
    else:
        for k in run: merged.append((items[k][0], items[k][1], sharps[k]))
    i = j
if stacks: log(f'{stacks} pile(s) de profondeur fusionnée(s) (EDF)')
items = [(p, t) for p, t, _ in merged]; sharps = [sh for _, _, sh in merged]

thr = max(3.0, 0.4 * float(np.median(sharps)))   # relative threshold: drops the blurred frames of this series only
log(f'netteté médiane {np.median(sharps):.1f}, seuil {thr:.1f}')
sel = []
last_pos = None
for (p, t), sharp in zip(items, sharps):
    if sharp < thr:
        continue
    pos = pos_at(t)
    if pos and last_pos and W0 and math.hypot(pos[0] - last_pos[0], pos[1] - last_pos[1]) < 0.15 * max(W0, H0):
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
def disc_mask(im):
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
    if (mask > 0).mean() < 0.97:   # disque oculaire visible : on recule du bord sombre
        er = max(10, int(0.03 * max(g.shape)))
        mask = cv2.erode(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (er, er)))
    return mask

# --- flat-field reel : images "vides" (fond clair, sans texture) filmees pendant la prise ---
flat = None
blank_idx = [i for i, (sh, (p, t)) in enumerate(zip(sharps, items)) if sh < 0.15 * float(np.median(sharps))]
if len(blank_idx) >= 3:
    cand = []
    for i in blank_idx[:12]:
        im = cv2.imread(items[i][0]).astype(np.float32)
        if im.mean() > 120: cand.append(im)
    if len(cand) >= 3:
        flat = np.median(np.stack(cand), axis=0)
        flat = cv2.GaussianBlur(flat, (0, 0), 2)
        log(f'flat-field mesuré sur {len(cand)} images de champ vide')
if flat is None:
    log('pas de champ vide filmé : vignettage estimé par lissage')

# --- aberration chromatique : echelle radiale des canaux R et B par rapport a G (optique fixe, estimee une fois) ---
def radial_scale(ch, s, c):
    M = np.float32([[s, 0, c[0] * (1 - s)], [0, s, c[1] * (1 - s)]])
    return cv2.warpAffine(ch, M, (ch.shape[1], ch.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
def chroma_estimate(paths):
    best = {'r': 1.0, 'b': 1.0}
    if a.no_chroma: return best
    grads = []
    for p in paths:
        im = cv2.imread(p); h, w = im.shape[:2]
        im = cv2.resize(im, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        grads.append(im)
    for key, idx in (('r', 2), ('b', 0)):
        scores = []
        cands = np.linspace(0.994, 1.006, 25)
        for s_ in cands:
            sc = 0.0
            for im in grads:
                h, w = im.shape[:2]; c = (w / 2, h / 2)
                g = cv2.Sobel(im[..., 1].astype(np.float32), cv2.CV_32F, 1, 1)
                ch = radial_scale(im[..., idx].astype(np.float32), s_, c)
                gc = cv2.Sobel(ch, cv2.CV_32F, 1, 1)
                # correlation des gradients hors du centre (la ou l'aberration agit)
                yy, xx = np.mgrid[0:h, 0:w]; ring = ((xx - c[0]) ** 2 + (yy - c[1]) ** 2) > (0.35 * max(h, w)) ** 2
                g1, g2 = g[ring], gc[ring]
                sc += float(np.corrcoef(g1, g2)[0, 1])
            scores.append(sc)
        best[key] = float(cands[int(np.argmax(scores))])
    return best

sharp_paths = [it['path'] for it in sorted(sel, key=lambda it: -it['sharp'])[:4]]
chroma = chroma_estimate(sharp_paths)
log(f"aberration chromatique : échelle R {chroma['r']:.4f}, B {chroma['b']:.4f}")

def preprocess(path, reg_size):
    im = cv2.imread(path)
    H_, W_ = im.shape[:2]
    if chroma['r'] != 1.0 or chroma['b'] != 1.0:
        c = (W_ / 2, H_ / 2)
        im = im.astype(np.float32)
        im[..., 2] = radial_scale(im[..., 2], chroma['r'], c); im[..., 0] = radial_scale(im[..., 0], chroma['b'], c)
        im = np.clip(im, 0, 255).astype(np.uint8)
    mask = disc_mask(im)
    ys, xs = np.where(mask > 0)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    crop, mask = im[y0:y1, x0:x1], mask[y0:y1, x0:x1]
    f32 = crop.astype(np.float32); m = (mask > 0).astype(np.float32)
    if flat is not None and flat.shape[:2] == (H_, W_):
        bg = flat[y0:y1, x0:x1]
    else:
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
    center = (W_ / 2 - x0, H_ / 2 - y0)          # centre optique dans le repere de la decoupe
    R = 0.5 * math.hypot(W_, H_)                  # rayon de normalisation de la distorsion
    return corr, mask, reg, rmask, s, (x0, y0), center, R

full_dir = os.path.join(out, 'full'); os.makedirs(full_dir, exist_ok=True)
regs, rmasks, scales, offsets, centers, Rs = [], [], [], [], [], []
for i, it in enumerate(sel):
    corr, mask, reg, rmask, s, off, center, R = preprocess(it['path'], a.reg_size)
    cv2.imwrite(os.path.join(full_dir, f'{i:04d}.jpg'), corr, [cv2.IMWRITE_JPEG_QUALITY, 97])
    cv2.imwrite(os.path.join(full_dir, f'{i:04d}_mask.png'), mask)
    regs.append(reg); rmasks.append(rmask); scales.append(s); offsets.append(off); centers.append(center); Rs.append(R)
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
pairs = {}   # (i,j) -> (pi, pj, A, n) en coordonnees de recalage (brutes)
for i, j in sorted(cand):
    if des[i] is None or des[j] is None or len(des[i]) < 20 or len(des[j]) < 20: continue
    mm = bf.knnMatch(des[i], des[j], k=2)
    good = [m for m, n in mm if m.distance < 0.75 * n.distance]
    if len(good) < 12: continue
    pi = np.float32([kps[i][m.queryIdx].pt for m in good]); pj = np.float32([kps[j][m.trainIdx].pt for m in good])
    A, inl = cv2.estimateAffinePartial2D(pj, pi, method=cv2.RANSAC, ransacReprojThreshold=4.0, maxIters=4000, confidence=0.999)
    if A is None: continue
    n_inl = int(inl.sum()); sc = math.hypot(A[0, 0], A[0, 1])
    if n_inl >= a.min_inliers and 0.5 < sc < 2.0:
        s_ = inl.ravel() > 0
        pairs[(i, j)] = (pi[s_], pj[s_], A, n_inl)
log(len(pairs), 'paires valides')
if not pairs:
    sys.exit('aucune paire d\'images ne se recouvre')

# --- distorsion radiale k1 (meme optique pour toutes les images) : choisie pour minimiser le residu des similitudes ---
def undist(pts, k, c, R):
    d = pts - np.float32(c); r2 = (d[:, 0] ** 2 + d[:, 1] ** 2) / (R * R)
    return np.float32(c) + d * (1 + k * r2)[:, None]
def pair_residual(k, subset):
    tot = []
    for (i, j) in subset:
        pi, pj, _, _ = pairs[(i, j)]
        ui = undist(pi, k, (centers[i][0] * scales[i], centers[i][1] * scales[i]), Rs[i] * scales[i])
        uj = undist(pj, k, (centers[j][0] * scales[j], centers[j][1] * scales[j]), Rs[j] * scales[j])
        A, _ = cv2.estimateAffinePartial2D(uj, ui, method=cv2.LMEDS)
        if A is None: continue
        d = cv2.transform(uj[None], A)[0] - ui
        tot.append(np.median(np.hypot(d[:, 0], d[:, 1])))
    return float(np.mean(tot)) if tot else 1e9
k1 = 0.0
if not a.no_distortion:
    subset = sorted(pairs, key=lambda k: -pairs[k][3])[:80]
    ks = np.linspace(-0.15, 0.15, 16)
    r = [pair_residual(k, subset) for k in ks]
    b = int(np.argmin(r)); lo, hi = ks[max(0, b - 1)], ks[min(len(ks) - 1, b + 1)]
    ks2 = np.linspace(lo, hi, 11); r2 = [pair_residual(k, subset) for k in ks2]
    cv2.setRNGSeed(0); r0 = pair_residual(0.0, subset)
    best = float(ks2[int(np.argmin(r2))])
    if min(r2) < 0.97 * r0:
        k1 = best
        log(f'distorsion radiale k1 = {k1:+.4f} (résidu {min(r2):.2f} px contre {r0:.2f} px sans correction)')
    else:
        log(f'distorsion radiale : aucune correction (résidu {r0:.2f} px, pas d\'amélioration nette)')
# points non distordus + similitudes recalculees
for (i, j) in list(pairs):
    pi, pj, A, n = pairs[(i, j)]
    ui = undist(pi, k1, (centers[i][0] * scales[i], centers[i][1] * scales[i]), Rs[i] * scales[i])
    uj = undist(pj, k1, (centers[j][0] * scales[j], centers[j][1] * scales[j]), Rs[j] * scales[j])
    A2, _ = cv2.estimateAffinePartial2D(uj, ui, method=cv2.LMEDS)
    pairs[(i, j)] = (ui, uj, A2 if A2 is not None else A, n)

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

# --- ajustement global : toutes les similitudes d'un coup, moindres carres robustes (Huber) ---
try:
    import scipy.sparse as sp, scipy.sparse.linalg as spl
    idx = {k: n for n, k in enumerate([k for k in comp if k != ref])}
    rows, cols, vals, rhs, wts = [], [], [], [], []
    eq = 0
    corr_list = []
    for (i, j), (pi, pj, _, _) in pairs.items():
        if i not in T or j not in T: continue
        n = min(len(pi), 120); step = max(1, len(pi) // n)
        for p, q in zip(pi[::step], pj[::step]):
            corr_list.append((i, j, p, q))
    def add(eq, k, x, y, sign):   # parametres (a,b,tx,ty) de l'image k ; eq paire (x_eq, y_eq)
        if k == ref:
            return (-sign * x, -sign * y)   # contribution connue : a=1,b=0,t=0 -> passe au second membre
        base = 4 * idx[k]
        rows.extend([eq, eq, eq, eq + 1, eq + 1, eq + 1]); cols.extend([base, base + 1, base + 2, base, base + 1, base + 3])
        vals.extend([sign * x, -sign * y, sign, sign * y, sign * x, sign])
        return (0.0, 0.0)
    for (i, j, p, q) in corr_list:
        r1 = add(eq, i, float(p[0]), float(p[1]), 1.0); r2 = add(eq, j, float(q[0]), float(q[1]), -1.0)
        rhs.extend([r1[0] + r2[0], r1[1] + r2[1]]); eq += 2
    Amat = sp.csr_matrix((vals, (rows, cols)), shape=(eq, 4 * len(idx)))
    bvec = np.array(rhs)
    x0 = np.zeros(4 * len(idx))
    for k, n in idx.items():
        x0[4 * n:4 * n + 4] = [T[k][0, 0], T[k][1, 0], T[k][0, 2], T[k][1, 2]]
    x = x0.copy(); w = np.ones(eq)
    for it in range(4):
        Wd = sp.diags(np.sqrt(w))
        sol = spl.lsqr(Wd @ Amat, Wd @ bvec, x0=x, atol=1e-8, btol=1e-8, iter_lim=2000)[0]
        x = sol
        res_v = Amat @ x - bvec
        rp = np.hypot(res_v[0::2], res_v[1::2]); delta = 2.0
        wp = np.where(rp <= delta, 1.0, delta / np.maximum(rp, 1e-6))
        w = np.repeat(wp, 2)
    for k, n in idx.items():
        a_, b_, tx, ty = x[4 * n:4 * n + 4]
        T[k] = np.array([[a_, -b_, tx], [b_, a_, ty], [0, 0, 1]])
    log(f'ajustement global : {len(corr_list)} correspondances, {len(idx)} images')
except ImportError:
    log('scipy absent : raffinement image par image')
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

# --- filet de securite : images non reliees mais suivies en direct, placees d'apres leur position ---
anchors = [k for k in comp if P[k]]
extra = 0
if anchors and W0:
    src = np.float32([P[k] for k in anchors]); dst = np.float32([(T[k] @ np.array([0, 0, 1]))[:2] for k in anchors])
    Apos = None
    if len(anchors) >= 3:
        Apos, _ = cv2.estimateAffinePartial2D(src, dst, method=cv2.LMEDS)
    if Apos is None:
        s_ = scales[ref]; off = dst.mean(0) - src.mean(0) * s_
        Apos = np.array([[s_, 0, off[0]], [0, s_, off[1]]], np.float64)
    for c in comps[1:]:
        for k in c:
            if P[k]:
                t = (Apos @ np.array([P[k][0], P[k][1], 1.0]))[:2]; sc = math.hypot(Apos[0, 0], Apos[0, 1])
                T[k] = np.array([[sc, 0, t[0]], [0, sc, t[1]], [0, 0, 1]]); comp.append(k); extra += 1
    if extra: log(f'{extra} image(s) placée(s) d\'après le suivi en direct (sans recalage fin)')

# --- compensation photometrique : gain par image et par canal, estime sur les recouvrements (log-gains, moindres carres) ---
gains = {k: np.ones(3, np.float32) for k in comp}
try:
    import scipy.sparse as sp, scipy.sparse.linalg as spl
    def patch_mean(img, pts, r=5):
        out = []
        for x, y in pts:
            x, y = int(round(x)), int(round(y))
            p = img[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1]
            out.append(p.reshape(-1, 3).mean(0) if p.size else np.array([np.nan] * 3))
        return np.array(out)
    gidx = {k: n for n, k in enumerate([k for k in comp if k != ref])}
    rows, cols, vals, rhs = [], [], [], []
    eq = 0
    for (i, j), (pi, pj, _, n) in pairs.items():
        if i not in T or j not in T: continue
        step = max(1, len(pi) // 40)
        mi = patch_mean(regs[i], pi[::step]); mj = patch_mean(regs[j], pj[::step])
        ok = np.all(np.isfinite(mi), 1) & np.all(np.isfinite(mj), 1) & (mi.min(1) > 20) & (mj.min(1) > 20)
        if ok.sum() < 5: continue
        d = np.log(mj[ok] + 1) - np.log(mi[ok] + 1)      # log(mean_j) - log(mean_i) par canal
        w = math.sqrt(ok.sum())
        for ch in range(3):
            # g_i - g_j = median(d)   (gain a appliquer a i moins celui de j)
            if i != ref: rows.append(eq); cols.append(3 * gidx[i] + ch); vals.append(w)
            if j != ref: rows.append(eq); cols.append(3 * gidx[j] + ch); vals.append(-w)
            rhs.append(w * float(np.median(d[:, ch]))); eq += 1
    if gidx and eq:
        # faible rappel vers 0 (pas de derive globale)
        for n in range(3 * len(gidx)):
            rows.append(eq); cols.append(n); vals.append(0.05); rhs.append(0.0); eq += 1
        Gm = sp.csr_matrix((vals, (rows, cols)), shape=(eq, 3 * len(gidx)))
        g = spl.lsqr(Gm, np.array(rhs), atol=1e-8, btol=1e-8)[0]
        for k, n in gidx.items():
            gains[k] = np.exp(np.clip(g[3 * n:3 * n + 3], -0.4, 0.4)).astype(np.float32)
        spread = max(float(np.abs(np.log(v)).max()) for v in gains.values())
        log(f'compensation photométrique : écart de gain max {spread * 100:.1f} %')
except ImportError:
    pass

res = []
for (i, j), (pi, pj, _, _) in pairs.items():
    if i in T and j in T:
        d = cv2.transform(pi[None], T[i][:2])[0] - cv2.transform(pj[None], T[j][:2])[0]
        res.append(np.median(np.hypot(d[:, 0], d[:, 1])))
log(f'recalage : erreur médiane {np.median(res):.2f} px (échelle recalage), max {np.max(res):.2f}')

# ---------------------------------------------------------------- 4. composition par tuiles
# transformation pleine resolution (coordonnees non distordues de l'image k -> reference en px de recalage)
Tf = {k: T[k] @ np.diag([scales[k], scales[k], 1]) for k in comp}
smin = min(math.hypot(Tf[k][0, 0], Tf[k][0, 1]) for k in comp)
Sref = np.diag([1 / smin, 1 / smin, 1])
sizes = {}
for k in comp:
    sizes[k] = cv2.imread(os.path.join(full_dir, f'{k:04d}_mask.png'), 0).shape
corners = []
for k in comp:
    h, w = sizes[k]
    corners.append(cv2.transform(np.float32([[0, 0], [w, 0], [w, h], [0, h]])[None], (Sref @ Tf[k])[:2])[0])
corners = np.vstack(corners); mn, mx = corners.min(0), corners.max(0)
W, H = mx - mn
shrink = min(1.0, a.max_side / max(W, H))
if shrink < 1: log(f'mosaïque réduite d\'un facteur {shrink:.2f} pour tenir dans {a.max_side} px')
Sfin = np.diag([shrink, shrink, 1]) @ np.array([[1, 0, -mn[0]], [0, 1, -mn[1]], [0, 0, 1]]) @ Sref
W, H = int(math.ceil(W * shrink)), int(math.ceil(H * shrink))
log('mosaïque', W, 'x', H)

# par image : matrice canvas<-undistorted (2x3), son inverse, boite englobante, poids (distance au bord, coeur net)
M, Minv, bbox, scale_k, dist_maps = {}, {}, {}, {}, {}
for k in comp:
    Mk = (Sfin @ Tf[k])
    M[k] = Mk[:2]; Minv[k] = np.linalg.inv(Mk)[:2]
    scale_k[k] = math.hypot(Mk[0, 0], Mk[0, 1])
    h, w = sizes[k]
    c4 = cv2.transform(np.float32([[0, 0], [w, 0], [w, h], [0, h]])[None], Mk[:2])[0]
    bbox[k] = (int(max(0, np.floor(c4[:, 0].min()))), int(max(0, np.floor(c4[:, 1].min()))), int(min(W, np.ceil(c4[:, 0].max()))), int(min(H, np.ceil(c4[:, 1].max()))))
    mk = cv2.imread(os.path.join(full_dir, f'{k:04d}_mask.png'), 0)
    dm = cv2.distanceTransform((mk > 0).astype(np.uint8), cv2.DIST_L2, 5)
    dist_maps[k] = (dm / max(float(dm.max()), 1.0)).astype(np.float32)   # 0 au bord, 1 au centre

def distort_pts(pu, c, R):
    """undistorted -> distorted (inverse de undist), par point fixe"""
    d = pu - np.float32(c); r2u = (d[..., 0] ** 2 + d[..., 1] ** 2) / (R * R)
    f = np.ones_like(r2u)
    for _ in range(4):
        f = 1.0 / (1.0 + k1 * r2u / (f * f))      # r_d = r_u / (1 + k1 r_d^2)
    return np.float32(c) + d * f[..., None]

TILE, MARGIN, LEVELS = a.tile, 256, 5
pano = np.zeros((H, W, 3), np.uint8); pano[:] = 255
covered = np.zeros((H, W), np.uint8)
imgs_cache = {}
def load(k):
    if k not in imgs_cache:
        if len(imgs_cache) > 40: imgs_cache.clear()
        imgs_cache[k] = cv2.imread(os.path.join(full_dir, f'{k:04d}.jpg'))
    return imgs_cache[k]

def blend_multiband(layers, masks):
    """layers: liste d'images float32 HxWx3 ; masks: liste de masques float32 HxW (partition) -> fondu multibande"""
    out = None
    for im, m in zip(layers, masks):
        gp = [im]; mp = [m]
        for _ in range(LEVELS):
            gp.append(cv2.pyrDown(gp[-1])); mp.append(cv2.pyrDown(mp[-1]))
        lp = [gp[i] - cv2.pyrUp(gp[i + 1], dstsize=(gp[i].shape[1], gp[i].shape[0])) for i in range(LEVELS)] + [gp[LEVELS]]
        contrib = [l * mm[..., None] for l, mm in zip(lp, mp)]
        out = contrib if out is None else [o + c for o, c in zip(out, contrib)]
    res = out[LEVELS]
    for i in range(LEVELS - 1, -1, -1):
        res = cv2.pyrUp(res, dstsize=(out[i].shape[1], out[i].shape[0])) + out[i]
    return res

ty_n, tx_n = math.ceil(H / TILE), math.ceil(W / TILE)
t_comp = time.time()
for ty in range(ty_n):
    for tx in range(tx_n):
        x0, y0 = tx * TILE, ty * TILE
        X0, Y0 = max(0, x0 - MARGIN), max(0, y0 - MARGIN)
        X1, Y1 = min(W, x0 + TILE + MARGIN), min(H, y0 + TILE + MARGIN)
        tw, th = X1 - X0, Y1 - Y0
        if tw <= 0 or th <= 0: continue
        ks = [k for k in comp if bbox[k][0] < X1 and bbox[k][2] > X0 and bbox[k][1] < Y1 and bbox[k][3] > Y0]
        if not ks: continue
        # grille des pixels de la tuile (centres)
        gx, gy = np.meshgrid(np.arange(X0, X1, dtype=np.float32), np.arange(Y0, Y1, dtype=np.float32))
        layers, weights = [], []
        for k in ks:
            Mi = Minv[k]
            ux = Mi[0, 0] * gx + Mi[0, 1] * gy + Mi[0, 2]; uy = Mi[1, 0] * gx + Mi[1, 1] * gy + Mi[1, 2]
            pd = distort_pts(np.stack([ux, uy], -1), centers[k], Rs[k])
            mapx, mapy = pd[..., 0].astype(np.float32), pd[..., 1].astype(np.float32)
            wm = cv2.remap(dist_maps[k], mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            if wm.max() <= 0: continue
            im = cv2.remap(load(k), mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(238, 238, 238)).astype(np.float32)
            im = np.clip(im * gains[k][None, None, :], 0, 255)
            valid = wm > 0.04                                                # on reste a l'ecart du bord sombre du disque
            im[~valid] = 238.0                                               # hors du disque : fond neutre (evite les halos du fondu)
            core = np.clip((wm - (1 - a.core)) / a.core, 0, 1) ** 3          # 1 au coeur, 0 vers le bord du champ
            wgt = (core + 1e-3 * valid) / (scale_k[k] ** 4)
            layers.append(im); weights.append(wgt.astype(np.float32))
        if not layers: continue
        Wst = np.stack(weights)
        lab = np.argmax(Wst, axis=0); anyc = Wst.max(0) > 0
        masks = [((lab == n) & anyc).astype(np.float32) for n in range(len(layers))]
        if len(layers) == 1:
            blended = layers[0]
        else:
            blended = blend_multiband(layers, masks)
        blended = np.clip(blended, 0, 255).astype(np.uint8)
        # ecriture de la zone centrale (sans la marge)
        ix0, iy0 = x0 - X0, y0 - Y0
        ix1, iy1 = min(tw, ix0 + TILE), min(th, iy0 + TILE)
        sub = blended[iy0:iy1, ix0:ix1]; subc = anyc[iy0:iy1, ix0:ix1]
        region = pano[y0:y0 + (iy1 - iy0), x0:x0 + (ix1 - ix0)]
        region[subc] = sub[subc]
        covered[y0:y0 + (iy1 - iy0), x0:x0 + (ix1 - ix0)] = subc.astype(np.uint8) * 255
log(f'composition : {ty_n * tx_n} tuiles en {time.time() - t_comp:.0f} s')
cv2.imwrite(os.path.join(out, 'pano.jpg'), pano, [cv2.IMWRITE_JPEG_QUALITY, 92])

fields, placements = [], {}
for k in comp:
    h, w = sizes[k]
    pts = cv2.transform(np.float32([[0, 0], [w, 0], [w, h], [0, h]])[None], M[k])[0]
    fields.append(dict(id=f'{k:04d}', pts=[[int(round(x)), int(round(y))] for x, y in pts], scale=round(1 / scale_k[k], 3)))
    placements[str(k)] = M[k].tolist()
json.dump(fields, open(os.path.join(out, 'fields.json'), 'w'))
json.dump(dict(size=[W, H], images=len(comp), of=N, placements=placements, k1=k1, chroma=chroma,
               flat_field='mesuré' if flat is not None else 'estimé', residual_px=float(np.median(res)),
               um_per_px=(float(meta['um_per_px']) * smin / (scales[ref] * shrink)) if meta.get('um_per_px') else None,   # µm par pixel de la mosaïque
               series=meta.get('series'), sources=[sel[k]['path'] for k in comp]),
          open(os.path.join(out, 'layout.json'), 'w'), indent=1)
log('terminé : pano.jpg', W, 'x', H, ',', len(comp), 'images utilisées sur', N)
