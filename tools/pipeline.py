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
import argparse, glob, itertools, json, math, os, subprocess, sys, time, threading
from concurrent.futures import ThreadPoolExecutor
import cv2, numpy as np
NWORK = max(2, min(16, os.cpu_count() or 2))   # OpenCV libere le GIL : les etapes lourdes tournent en parallele sur les coeurs

ap = argparse.ArgumentParser()
ap.add_argument('series')
ap.add_argument('--out', default=None)
ap.add_argument('--fps', type=float, default=4.0)
ap.add_argument('--blur', type=float, default=25.0)
ap.add_argument('--max-frames', type=int, default=600)
ap.add_argument('--reg-size', type=int, default=1200, help='plus grand cote des images pour le recalage')
ap.add_argument('--max-side', type=int, default=32000, help='plus grand cote de la mosaique finale (au-dela, reduction : perte de finesse)')
ap.add_argument('--min-inliers', type=int, default=40)
ap.add_argument('--core', type=float, default=0.70, help='fraction du rayon de champ utilisee en priorite (coeur net)')
ap.add_argument('--no-distortion', action='store_true', help='ne pas estimer la distorsion radiale')
ap.add_argument('--no-chroma', action='store_true', help='ne pas corriger l aberration chromatique')
ap.add_argument('--tile', type=int, default=2048, help='taille des tuiles de composition')
ap.add_argument('--no-seams', action='store_true', help='coutures a mi-chemin (lignes droites) au lieu des coutures qui suivent le fond')
ap.add_argument('--depth', choices=['auto', 'off', 'on'], default='auto', help='piles de profondeur (EDF + plans) : off = image la plus nette a chaque arret ; auto = selon serie.json (depth), off par defaut')
ap.add_argument('--segment', type=int, default=0, help='segment d objectif de la prise (0 = premier) ; voir meta.segments')
ap.add_argument('--component', type=int, default=0, help='composante connexe a assembler (0 = la plus grande) ; les autres sont listees dans layout.json')
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
# --- phases marquees pendant la prise : fond blanc (flat-field) et segments d'objectif ---
blank_phases = [ph for ph in (meta.get('phases') or []) if ph.get('kind') == 'blank' and ph.get('t1') is not None]
in_blank = lambda t: t is not None and any(ph['t0'] <= t <= ph['t1'] for ph in blank_phases)
blank_items = [(p, t) for p, t in items if in_blank(t)]
segs = list(meta.get('segments') or [])
if segs:
    if a.segment >= len(segs): sys.exit(f'segment {a.segment} inexistant')
    sg = segs[a.segment]; t0s, t1s = sg.get('t0') or 0, sg.get('t1') or 1e12
    items = [(p, t) for p, t in items if t is not None and t0s <= t <= t1s and not in_blank(t)]
    meta['objective'] = sg.get('objective') or meta.get('objective')
    if sg.get('um_per_px'): meta['um_per_px'] = sg['um_per_px']
    log(f'segment {a.segment + 1}/{len(segs)} : objectif {meta["objective"]}, {len(items)} images entre {t0s / 1000:.0f} s et {min(t1s, 1e9) / 1000:.0f} s')
else:
    items = [(p, t) for p, t in items if not in_blank(t)]
if blank_items: log(f'{len(blank_items)} image(s) de fond blanc marquées pendant la prise')
if not items:
    sys.exit('aucune image dans ce segment')
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
def sharpness(g):
    """(variance du laplacien, mesure normalisee par le contenu : energie haute frequence / energie du gradient)"""
    lap = cv2.Laplacian(g, cv2.CV_64F); gx = cv2.Sobel(g, cv2.CV_64F, 1, 0); gy = cv2.Sobel(g, cv2.CV_64F, 0, 1)
    return float(lap.var()), float(lap.var() / (gx.var() + gy.var() + 1e-6))
sharps, sharpn, smalls = [], [], []
for p, t in items:
    g = cv2.imread(p, cv2.IMREAD_REDUCED_GRAYSCALE_4)
    sv, sn = sharpness(g); sharps.append(sv); sharpn.append(sn)
    smalls.append(cv2.resize(g, (256, 256), interpolation=cv2.INTER_AREA).astype(np.float32))
h4, w4 = cv2.imread(items[0][0], cv2.IMREAD_REDUCED_GRAYSCALE_4).shape
win = cv2.createHanningWindow((256, 256), cv2.CV_32F)
# deplacement image a image, en fraction du cadre. Pendant un balayage de mise au point la correlation peut
# echouer (image floue) : on se rabat sur le suivi du telephone, sinon on suppose "immobile" si l'une des deux
# images est floue (c'est la vis micrometrique, pas la platine).
med_sharp0 = float(np.median(sharps))
moves, vecs = [0.0], [(0.0, 0.0)]
for i in range(1, len(smalls)):
    (dx, dy), resp = cv2.phaseCorrelate(smalls[i - 1], smalls[i], win)
    if resp > 0.05:
        v = (dx / 256, dy / 256)
    else:
        p0, p1 = pos_at(items[i - 1][1]), pos_at(items[i][1])
        if p0 and p1 and W0:
            v = ((p1[0] - p0[0]) / max(W0, H0), (p1[1] - p0[1]) / max(W0, H0))
        elif min(sharps[i - 1], sharps[i]) < 0.5 * med_sharp0:
            v = (0.0, 0.0)
        else:
            v = (1.0, 0.0)
    vecs.append(v); moves.append(math.hypot(*v))
if not videos:   # photos a intervalle : pas de pile de profondeur, chaque photo est un champ
    moves = [1.0] * len(moves); vecs = [(1.0, 0.0)] * len(vecs)
del smalls

# ---- piles de profondeur : suites d'images immobiles dont la nettete varie (vis micrometrique tournee) ----
def align_stack(imgs):
    """Decalages (dx, dy) de chaque plan vers le plan le plus net, robustes au flou : ECC en translation sur les gradients
    a basse resolution, initialise par le plan precedent (la derive de la vis s'accumule). Retourne (shifts, ok)."""
    n = len(imgs); H_, W_ = imgs[0].shape[:2]
    f = min(1.0, 400.0 / max(H_, W_))
    g = [cv2.resize(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), None, fx=f, fy=f, interpolation=cv2.INTER_AREA).astype(np.float32) for im in imgs]
    def feat(x):   # gradients lisses : comparables entre un plan net et un plan flou
        x = cv2.GaussianBlur(x, (0, 0), 2.0)
        return cv2.magnitude(cv2.Sobel(x, cv2.CV_32F, 1, 0), cv2.Sobel(x, cv2.CV_32F, 0, 1))
    F = [feat(x) for x in g]
    sharp = [float(cv2.Laplacian(x, cv2.CV_32F).var()) for x in g]
    ref = int(np.argmax(sharp))
    shifts, ok = [(0.0, 0.0)] * n, [False] * n
    ok[ref] = True
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-4)
    lim = 0.2 * max(g[0].shape)
    for order in ([range(ref + 1, n), range(ref - 1, -1, -1)]):
        prev = (0.0, 0.0)
        for k in order:
            best = None
            for init in ([prev, (0.0, 0.0)] if prev != (0.0, 0.0) else [(0.0, 0.0)]):
                W = np.array([[1, 0, init[0]], [0, 1, init[1]]], np.float32)
                try:
                    cc, W = cv2.findTransformECC(F[ref], F[k], W, cv2.MOTION_TRANSLATION, crit, None, 5)
                except cv2.error:
                    continue
                if abs(W[0, 2]) < lim and abs(W[1, 2]) < lim and (best is None or cc > best[0]): best = (cc, float(W[0, 2]), float(W[1, 2]))
            if best and best[0] > 0.25:
                shifts[k] = (best[1] / f, best[2] / f); ok[k] = True; prev = (best[1], best[2])
            else:
                ok[k] = False   # plan non alignable : exclu de la fusion
    return shifts, ok, ref

def edf(paths):
    """Fusion de plans de mise au point : en chaque pixel on garde le plan le plus net (pyramide laplacienne).
    Retourne (image fusionnee, plans alignes uint8, cartes de nettete reduites). Les plans qui ne s'alignent pas sur
    le plan le plus net sont ecartes (sinon ils laissent des fantomes)."""
    imgs = [cv2.imread(q) for q in paths]
    shifts, oks, ref = align_stack(imgs)
    keep = [k for k in range(len(imgs)) if oks[k]]
    if len(keep) < len(imgs): log(f'  pile : {len(imgs) - len(keep)} plan(s) non alignable(s) écarté(s)')
    aligned, maps, valids = [], [], []
    refim = imgs[ref]
    for k in keep:
        im = imgs[k]; sx, sy = shifts[k]
        valid = np.ones(im.shape[:2], np.float32)
        if abs(sx) > 0.5 or abs(sy) > 0.5:
            M = np.float32([[1, 0, sx], [0, 1, sy]])
            im = cv2.warpAffine(im, M, (im.shape[1], im.shape[0]), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))   # convention ECC
            valid = cv2.warpAffine(valid, M, (im.shape[1], im.shape[0]), flags=cv2.INTER_NEAREST + cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            valid = cv2.erode(valid, np.ones((9, 9), np.uint8))          # on s'ecarte du bord du plan decale
            im = np.where(valid[..., None] > 0, im, refim)                # hors champ : pixels du plan de reference
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32)
        aligned.append(im); valids.append(valid)
        maps.append(cv2.GaussianBlur(np.abs(cv2.Laplacian(g, cv2.CV_32F, ksize=3)), (0, 0), 6) * valid)   # pas de poids hors champ
    M = np.stack(maps); M = (M / (M.max(0, keepdims=True) + 1e-6)) ** 8; M = M / (M.sum(0, keepdims=True) + 1e-9)
    small_maps = [cv2.resize(m, (64, 48), interpolation=cv2.INTER_AREA) for m in maps]
    levels = 5
    out_pyr = None
    for im, w in zip(aligned, M):
        gp = [im.astype(np.float32)]; wp = [w]
        for _ in range(levels):
            gp.append(cv2.pyrDown(gp[-1])); wp.append(cv2.pyrDown(wp[-1]))
        lp = [gp[i] - cv2.pyrUp(gp[i + 1], dstsize=(gp[i].shape[1], gp[i].shape[0])) for i in range(levels)] + [gp[levels]]
        contrib = [l * wl[..., None] for l, wl in zip(lp, wp)]
        out_pyr = contrib if out_pyr is None else [o + c for o, c in zip(out_pyr, contrib)]
    res = out_pyr[levels]
    for i in range(levels - 1, -1, -1):
        res = cv2.pyrUp(res, dstsize=(out_pyr[i].shape[1], out_pyr[i].shape[0])) + out_pyr[i]
    return np.clip(res, 0, 255).astype(np.uint8), aligned, small_maps

def spectral_order(maps):
    """Ordonne des plans de mise au point sans connaitre z : les plans voisins en z ont des cartes de nettete
    semblables ; le vecteur de Fiedler du graphe de similarite donne un ordre 1-D (seriation spectrale).
    Marche aussi si la vis a fait un aller-retour."""
    n = len(maps)
    if n <= 2: return list(range(n))
    X = np.stack([m.ravel() for m in maps]).astype(np.float64)
    X -= X.mean(1, keepdims=True); X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    Sm = np.clip(X @ X.T, 0, 1); np.fill_diagonal(Sm, 0)
    Lap = np.diag(Sm.sum(1)) - Sm
    w, v = np.linalg.eigh(Lap)
    return [int(i) for i in np.argsort(v[:, 1])]

ZMAX = 5   # plans conserves au plus par pile (z-stack navigable)

DEPTH = a.depth if a.depth != 'auto' else (str(meta.get('depth') or 'off'))
if DEPTH != 'on': log('profondeur désactivée : à chaque arrêt de platine, l\'image la plus nette est gardée')
STILL, DRIFT = 0.06, 0.15   # pas < 6 % du cadre et derive cumulee < 15 % depuis le debut de l'arret = platine immobile
merged, i, stacks = [], 0, 0
while i < len(items):
    j = i + 1; cx = cy = 0.0
    while j < len(items) and moves[j] < STILL:
        cx += vecs[j][0]; cy += vecs[j][1]
        if math.hypot(cx, cy) >= DRIFT: break
        j += 1
    run = list(range(i, j))
    if len(run) >= 3:
        sh = [sharps[k] for k in run]
        if DEPTH == 'on' and max(sh) > 1.5 * min(sh) and max(sh) >= 3.0:
            outp = os.path.join(frames_dir, f'z_{i:06d}.jpg')
            fused, aligned, smaps = edf([items[k][0] for k in run])
            cv2.imwrite(outp, fused, [cv2.IMWRITE_JPEG_QUALITY, 95])
            # plans conserves pour la navigation en profondeur : ordre z retrouve par seriation, ZMAX plans repartis
            order = spectral_order(smaps)
            L = min(ZMAX, len(order))
            pick = [order[int(round(q * (len(order) - 1) / max(1, L - 1)))] for q in range(L)]
            planes = []
            for z, k in enumerate(pick):
                pp = os.path.join(frames_dir, f'z_{i:06d}_p{z}.jpg')
                cv2.imwrite(pp, aligned[k], [cv2.IMWRITE_JPEG_QUALITY, 95]); planes.append(pp)
            del aligned
            sn = sharpness(cv2.resize(cv2.cvtColor(fused, cv2.COLOR_BGR2GRAY), (fused.shape[1] // 4, fused.shape[0] // 4), interpolation=cv2.INTER_AREA))[1]
            merged.append((outp, items[run[len(run) // 2]][1], max(sh), sn, planes)); stacks += 1
        else:
            k = max(run, key=lambda k: sharps[k]); merged.append((items[k][0], items[k][1], sharps[k], sharpn[k], None))
    else:
        for k in run: merged.append((items[k][0], items[k][1], sharps[k], sharpn[k], None))
    i = j
if stacks: log(f'{stacks} pile(s) de profondeur fusionnée(s) (EDF), plans conservés pour la navigation')
planes_of = {p: pl for p, _, _, _, pl in merged if pl}
sharpn_of = {p: sn for p, _, _, sn, _ in merged}
items = [(p, t) for p, t, _, _, _ in merged]; sharps = [sh for _, _, sh, _, _ in merged]
n_candidates = len(items)

thr = max(3.0, 0.4 * float(np.median(sharps)))   # relative threshold: drops the blurred frames of this series only
log(f'netteté médiane {np.median(sharps):.1f}, seuil {thr:.1f}')
sel = []
last_pos = None
for (p, t), sharp in zip(items, sharps):
    if sharp < thr:
        continue
    pos = pos_at(t)
    planes = planes_of.get(p)
    if pos and last_pos and W0 and math.hypot(pos[0] - last_pos[0], pos[1] - last_pos[1]) < 0.15 * max(W0, H0):
        if planes and sel and not sel[-1].get('planes'): sel.pop()     # une pile de profondeur remplace l'image simple prise au meme endroit
        else: continue
    if pos: last_pos = pos
    sel.append(dict(path=p, t=t, sharp=sharp, sharpn=sharpn_of[p], pos=pos, planes=planes))
n_blurred = sum(1 for sh in sharps if sh < thr)
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
if blank_items:
    cand = [cv2.imread(p).astype(np.float32) for p, _ in blank_items[:16]]
    cand = [im for im in cand if im.mean() > 100]
    if len(cand) >= 2:
        flat = cv2.GaussianBlur(np.median(np.stack(cand), axis=0), (0, 0), 2)
        log(f'flat-field mesuré sur {len(cand)} images de fond blanc (phase marquée)')
blank_idx = [i for i, (sh, (p, t)) in enumerate(zip(sharps, items)) if sh < 0.15 * float(np.median(sharps))]
if flat is None and len(blank_idx) >= 3:
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

def white_point(img, mask):
    """Blanc d'une image : moyenne par canal des memes pixels, les plus lumineux du champ (fond), et non un
    percentile par canal (qui melange des pixels differents et teinte les champs charges en cellules)."""
    lum = img[..., 0] * 0.114 + img[..., 1] * 0.587 + img[..., 2] * 0.299
    m = mask > 0
    if m.sum() < 100: return np.array([238.0, 238.0, 238.0])
    thr = np.percentile(lum[m], 97)
    sel_ = m & (lum >= thr)
    return np.array([img[..., c][sel_].mean() for c in range(3)])

WHITE = {}
def white_of(path): return WHITE[path]

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
    wb = white_of(path) if path in WHITE else white_point(corr, mask)
    corr = np.clip(corr * (238.0 / np.maximum(wb, 1)), 0, 255).astype(np.uint8)
    corr[mask == 0] = 0
    s = min(1.0, reg_size / max(corr.shape[:2]))
    reg = cv2.resize(corr, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else corr
    rmask = cv2.resize(mask, (reg.shape[1], reg.shape[0]), interpolation=cv2.INTER_NEAREST)
    center = (W_ / 2 - x0, H_ / 2 - y0)          # centre optique dans le repere de la decoupe
    R = 0.5 * math.hypot(W_, H_)                  # rayon de normalisation de la distorsion
    return corr, mask, reg, rmask, s, (x0, y0), center, R

def preprocess_plane(path, crop, mask, edf_path=None):
    """Meme chaine que preprocess, mais avec la decoupe et le masque de l'image EDF (meme geometrie)."""
    im = cv2.imread(path)
    H_, W_ = im.shape[:2]
    if chroma['r'] != 1.0 or chroma['b'] != 1.0:
        c = (W_ / 2, H_ / 2)
        im = im.astype(np.float32)
        im[..., 2] = radial_scale(im[..., 2], chroma['r'], c); im[..., 0] = radial_scale(im[..., 0], chroma['b'], c)
        im = np.clip(im, 0, 255).astype(np.uint8)
    x0, y0, w, h = crop
    f32 = im[y0:y0 + h, x0:x0 + w].astype(np.float32); m = (mask > 0).astype(np.float32)
    if flat is not None and flat.shape[:2] == (H_, W_):
        bg = flat[y0:y0 + h, x0:x0 + w]
    else:
        small = cv2.resize(f32 * m[..., None], None, fx=1 / 16, fy=1 / 16, interpolation=cv2.INTER_AREA)
        msmall = cv2.resize(m, None, fx=1 / 16, fy=1 / 16, interpolation=cv2.INTER_AREA)
        sig = max(h, w) * 0.12 / 16
        bgs = cv2.GaussianBlur(small, (0, 0), sig) / (cv2.GaussianBlur(msmall, (0, 0), sig)[..., None] + 1e-6)
        bg = cv2.resize(bgs, (w, h), interpolation=cv2.INTER_LINEAR)
    ref = np.array([np.percentile(bg[..., c][mask > 0], 90) for c in range(3)])
    corr = np.clip(f32 * (ref / (bg + 1e-6)), 0, 255)
    wb = WHITE.get(edf_path) if edf_path else None
    if wb is None: wb = white_point(corr, mask)
    corr = np.clip(corr * (238.0 / np.maximum(wb, 1)), 0, 255).astype(np.uint8)
    corr[mask == 0] = 0
    return corr

# --- balance des blancs : estimee sur chaque image (reduite), puis lissee sur les images voisines dans le temps ---
def _white_job(it):
    im4 = cv2.imread(it['path'], cv2.IMREAD_REDUCED_COLOR_4)
    m4 = disc_mask(im4)
    f4 = im4.astype(np.float32)
    if flat is not None:
        bg4 = cv2.resize(flat, (im4.shape[1], im4.shape[0]), interpolation=cv2.INTER_AREA)
        ref4 = np.array([np.percentile(bg4[..., c][m4 > 0], 90) for c in range(3)])
        f4 = np.clip(f4 * (ref4 / (bg4 + 1e-6)), 0, 255)
    return white_point(f4, m4)
with ThreadPoolExecutor(NWORK) as ex:
    _w = np.array(list(ex.map(_white_job, sel)))
k_ = min(9, len(_w) if len(_w) % 2 else len(_w) - 1)
for i_ in range(len(_w)):
    lo_, hi_ = max(0, i_ - k_ // 2), min(len(_w), i_ + k_ // 2 + 1)
    WHITE[sel[i_]['path']] = np.median(_w[lo_:hi_], axis=0)
if len(_w) > 1:
    drift = float(np.max(_w.max(0) / np.maximum(_w.min(0), 1) - 1)) * 100
    log(f'balance des blancs : blanc estimé par image, lissé sur {k_} images (variation max {drift:.0f} %)')

full_dir = os.path.join(out, 'full'); os.makedirs(full_dir, exist_ok=True)
def _pre_job(i):
    it = sel[i]
    corr, mask, reg, rmask, s, off, center, R = preprocess(it['path'], a.reg_size)
    cv2.imwrite(os.path.join(full_dir, f'{i:04d}.jpg'), corr, [cv2.IMWRITE_JPEG_QUALITY, 97])
    cv2.imwrite(os.path.join(full_dir, f'{i:04d}_mask.png'), mask)
    it['crop'] = [int(off[0]), int(off[1]), int(corr.shape[1]), int(corr.shape[0])]
    for z, pp in enumerate(it.get('planes') or []):
        cv2.imwrite(os.path.join(full_dir, f'{i:04d}_z{z}.jpg'), preprocess_plane(pp, it['crop'], mask, it['path']), [cv2.IMWRITE_JPEG_QUALITY, 97])
    return reg, rmask, s, off, center, R
with ThreadPoolExecutor(NWORK) as ex:
    _res = list(ex.map(_pre_job, range(len(sel))))
regs = [r[0] for r in _res]; rmasks = [r[1] for r in _res]; scales = [r[2] for r in _res]; offsets = [r[3] for r in _res]; centers = [r[4] for r in _res]; Rs = [r[5] for r in _res]
del _res
N = len(regs)
log('prétraitement terminé,', N, 'images, recalage à', regs[0].shape[1], 'x', regs[0].shape[0])

# ---------------------------------------------------------------- 3. recalage
def _sift_job(args_):
    reg, m = args_
    g = cv2.createCLAHE(2.0, (8, 8)).apply(cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY))
    return cv2.SIFT_create(5000).detectAndCompute(g, cv2.erode(m, np.ones((9, 9), np.uint8)))
with ThreadPoolExecutor(NWORK) as ex:
    _kd = list(ex.map(_sift_job, zip(regs, rmasks)))
kps = [k for k, _ in _kd]; des = [d for _, d in _kd]
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

def _pair_job(ij):
    i, j = ij
    if des[i] is None or des[j] is None or len(des[i]) < 20 or len(des[j]) < 20: return None
    mm = cv2.BFMatcher(cv2.NORM_L2).knnMatch(des[i], des[j], k=2)
    good = [m for m, n in mm if m.distance < 0.75 * n.distance]
    if len(good) < 12: return None
    pi = np.float32([kps[i][m.queryIdx].pt for m in good]); pj = np.float32([kps[j][m.trainIdx].pt for m in good])
    A, inl = cv2.estimateAffinePartial2D(pj, pi, method=cv2.RANSAC, ransacReprojThreshold=4.0, maxIters=4000, confidence=0.999)
    if A is None: return None
    n_inl = int(inl.sum()); sc = math.hypot(A[0, 0], A[0, 1])
    if n_inl >= a.min_inliers and 0.5 < sc < 2.0:
        s_ = inl.ravel() > 0
        return (pi[s_], pj[s_], A, n_inl)
    return None
pairs = {}   # (i,j) -> (pi, pj, A, n) en coordonnees de recalage (brutes)
_cl = sorted(cand)
with ThreadPoolExecutor(NWORK) as ex:
    for ij, r_ in zip(_cl, ex.map(_pair_job, _cl)):
        if r_ is not None: pairs[ij] = r_
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
if a.component >= len(comps): sys.exit(f'composante {a.component} inexistante')
comp = comps[a.component]
# zones distinctes dans une meme prise (plusieurs details au fort grossissement) : les autres composantes assez grandes
# une zone distincte = composante assez grande ET, quand le suivi donne des positions, eloignee de la composante
# principale (sinon c'est un fragment du meme balayage, qui sera place d'apres le suivi). Au plus 4 zones.
def comp_center(c):
    pts = [P[k] for k in c if P[k]]
    return np.mean(pts, axis=0) if pts else None
main_c = comps[a.component]
zone_ids = []
for i_, c in enumerate(comps):
    if i_ == a.component or len(c) < max(3, int(0.15 * len(main_c))): continue
    if W0 and any(P[k] for k in c) and any(P[k] for k in main_c):
        cm, cc = comp_center(main_c), comp_center(c)
        far = np.hypot(*(cc - cm)) > 1.5 * max(W0, H0)
        # fragment si une image de c est a moins d'un champ d'une image de la principale
        near = any(P[k] and P[j] and math.hypot(P[k][0] - P[j][0], P[k][1] - P[j][1]) < 1.0 * max(W0, H0) for k in c for j in main_c)
        if near or not far: continue
    zone_ids.append(i_)
zone_ids = zone_ids[:4]
other_components = [len(comps[i_]) for i_ in zone_ids]
is_zone = set(zone_ids)
log('composantes :', [len(c) for c in comps], f'-> on assemble la n°{a.component} :', len(comp), 'images')
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
linked = set(comp)   # images recalees par les points ; les suivantes ne le sont que par le suivi en direct
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
    for ci, c in enumerate(comps):
        if ci == a.component or ci in is_zone: continue    # les zones distinctes sont assemblees a part ; le reste est un fragment
        for k in c:
            if P[k]:
                # Apos convertit les positions du suivi (px du flux) en px de recalage de la reference : seule la
                # translation sert ; l'image garde l'echelle de la reference (toutes les images ont la meme echelle de recalage)
                t = (Apos @ np.array([P[k][0], P[k][1], 1.0]))[:2]
                T[k] = np.array([[1.0, 0, t[0]], [0, 1.0, t[1]], [0, 0, 1]]); comp.append(k); extra += 1
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
imgs_cache = {}; _cache_lock = threading.Lock()
def load(k):
    with _cache_lock:
        im = imgs_cache.get(k)
    if im is None:
        im = cv2.imread(os.path.join(full_dir, f'{k:04d}.jpg'))
        with _cache_lock:
            if len(imgs_cache) > 60: imgs_cache.clear()
            imgs_cache[k] = im
    return im

SEAM_SCALE = 0.25
def seam_masks(layers, masks, valids, lab, anyc):
    """Coutures qui suivent le fond (programmation dynamique sur couleur + gradient, OpenCV stitching) au lieu
    de lignes droites a mi-chemin : un residu de recalage de quelques pixels devient invisible. Calcul a 1/4,
    masques remontes a la taille de la tuile ; chaque pixel reste attribue a exactement une image."""
    n = len(layers)
    # seules les images qui gagnent quelque part comptent (les autres ne contribueraient jamais)
    keep = [i for i in range(n) if masks[i].any()]
    if len(keep) < 2: return masks
    h, w = lab.shape
    sw, sh = max(8, int(w * SEAM_SCALE)), max(8, int(h * SEAM_SCALE))
    imgs_s, msk_s = [], []
    for i in keep:
        imgs_s.append(cv2.resize(layers[i], (sw, sh), interpolation=cv2.INTER_AREA))
        # region admissible = zone valide de l'image, un peu erodee pour rester loin de son bord
        v = cv2.erode(valids[i].astype(np.uint8), np.ones((9, 9), np.uint8))
        msk_s.append(cv2.resize(v, (sw, sh), interpolation=cv2.INTER_NEAREST) * 255)
    try:
        try: finder = cv2.detail_DpSeamFinder('COLOR_GRAD')          # OpenCV 4.x/5.x : nom de la fonction de cout
        except Exception: finder = cv2.detail_DpSeamFinder(cv2.detail.DpSeamFinder_COLOR_GRAD)
        out = finder.find(imgs_s, [(0, 0)] * len(keep), msk_s)
        if out is None: out = msk_s
        out = [np.asarray(m.get() if hasattr(m, 'get') else m) for m in out]
    except (cv2.error, TypeError) as e:
        if not getattr(seam_masks, 'warned', False): log(f'coutures : OpenCV a refusé ({e}), lignes droites'); seam_masks.warned = True
        return masks
    # remontee + attribution unique : pixel pris par exactement une image -> elle ; sinon le gagnant du poids
    full = [cv2.resize((m > 0).astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0 for m in out]
    cnt = np.zeros((h, w), np.int32)
    for f in full: cnt += f
    new_lab = lab.copy()
    for idx, i in enumerate(keep):
        new_lab[(cnt == 1) & full[idx]] = i
    res = [np.zeros((h, w), np.float32) for _ in range(n)]
    for i in range(n):
        res[i][(new_lab == i) & anyc] = 1.0
    return res

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
def compose_tile(tx, ty, loader, only=None):
    """Compose la tuile (tx, ty) ; loader(k) donne l'image pleine resolution de k. Retourne (x0, y0, image, couverture) ou None.
    Deux passes : d'abord les poids (legers) pour savoir quelles images gagnent quelque part, puis seulement
    celles-la sont remappees en pleine resolution (memoire divisee par 3 a 5 sur une video tres recouvrante)."""
    x0, y0 = tx * TILE, ty * TILE
    X0, Y0 = max(0, x0 - MARGIN), max(0, y0 - MARGIN)
    X1, Y1 = min(W, x0 + TILE + MARGIN), min(H, y0 + TILE + MARGIN)
    tw, th = X1 - X0, Y1 - Y0
    if tw <= 0 or th <= 0: return None
    ks = [k for k in comp if bbox[k][0] < X1 and bbox[k][2] > X0 and bbox[k][1] < Y1 and bbox[k][3] > Y0]
    if not ks or (only is not None and not any(k in only for k in ks)): return None
    gx, gy = np.meshgrid(np.arange(X0, X1, dtype=np.float32), np.arange(Y0, Y1, dtype=np.float32))
    maps, weights, valids, kept = {}, [], [], []
    for k in ks:
        Mi = Minv[k]
        ux = Mi[0, 0] * gx + Mi[0, 1] * gy + Mi[0, 2]; uy = Mi[1, 0] * gx + Mi[1, 1] * gy + Mi[1, 2]
        pd = distort_pts(np.stack([ux, uy], -1), centers[k], Rs[k])
        mapx, mapy = pd[..., 0].astype(np.float32), pd[..., 1].astype(np.float32)
        wm = cv2.remap(dist_maps[k], mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        if wm.max() <= 0: continue
        valid = wm > 0.04                                                # on reste a l'ecart du bord sombre du disque
        core = np.clip((wm - (1 - a.core)) / a.core, 0, 1) ** 3          # 1 au coeur, 0 vers le bord du champ
        wgt = ((core + 1e-3 * valid) / (scale_k[k] ** 4)).astype(np.float32)
        maps[k] = (mapx, mapy); weights.append(wgt); valids.append(valid); kept.append(k)
    del gx, gy
    if not kept: return None
    Wst = np.stack(weights)
    lab0 = np.argmax(Wst, axis=0); anyc = Wst.max(0) > 0
    del Wst
    win = [n for n in range(len(kept)) if ((lab0 == n) & anyc).any()]   # images qui gagnent quelque part
    lab = np.full(lab0.shape, -1, np.int32)
    for new_n, n in enumerate(win): lab[(lab0 == n) & anyc] = new_n
    layers, vals = [], []
    for n in win:
        k = kept[n]; mapx, mapy = maps[k]
        im = cv2.remap(loader(k), mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(238, 238, 238)).astype(np.float32)
        im = np.clip(im * gains[k][None, None, :], 0, 255)
        im[~valids[n]] = 238.0                                           # hors du disque : fond neutre (evite les halos du fondu)
        layers.append(im); vals.append(valids[n])
    del maps, weights
    masks = [((lab == n) & anyc).astype(np.float32) for n in range(len(layers))]
    if len(layers) > 1 and not a.no_seams:
        masks = seam_masks(layers, masks, vals, lab, anyc)
    blended = layers[0] if len(layers) == 1 else blend_multiband(layers, masks)
    blended = np.clip(blended, 0, 255).astype(np.uint8)
    ix0, iy0 = x0 - X0, y0 - Y0
    ix1, iy1 = min(tw, ix0 + TILE), min(th, iy0 + TILE)
    return x0, y0, blended[iy0:iy1, ix0:ix1], anyc[iy0:iy1, ix0:ix1]

t_comp = time.time()
_tiles = [(tx, ty) for ty in range(ty_n) for tx in range(tx_n)]
try:
    _avail_gb = float(open('/proc/meminfo').read().split('MemAvailable:')[1].split()[0]) / 1e6
except Exception:
    _avail_gb = 8.0
TILE_WORKERS = max(1, min(NWORK, 6, int(_avail_gb / 2.0)))   # ~2 Go par tuile de 2560 px en pleine charge
with ThreadPoolExecutor(TILE_WORKERS) as ex:
    for r in ex.map(lambda t: compose_tile(t[0], t[1], load), _tiles):
        if r is None: continue
        x0, y0, sub, subc = r
        region = pano[y0:y0 + sub.shape[0], x0:x0 + sub.shape[1]]
        region[subc] = sub[subc]
        covered[y0:y0 + sub.shape[0], x0:x0 + sub.shape[1]] = subc.astype(np.uint8) * 255
log(f'composition : {ty_n * tx_n} tuiles en {time.time() - t_comp:.0f} s')
cv2.imwrite(os.path.join(out, 'pano.jpg'), pano, [cv2.IMWRITE_JPEG_QUALITY, 96])

# --- z-stack navigable : une mosaique par plan, seules les tuiles touchant une pile sont recomposees ---
zfields = [k for k in comp if sel[k].get('planes')]
ZL = max((len(sel[k]['planes']) for k in zfields), default=0)
if zfields and ZL >= 2:
    t_z = time.time()
    zset = set(zfields)
    for z in range(ZL):
        zcache = {}; zlock = threading.Lock()
        def load_z(k, z=z):
            if k not in zset: return load(k)
            n = len(sel[k]['planes']); zi = int(round(z * (n - 1) / (ZL - 1)))
            key = (k, zi)
            with zlock: im = zcache.get(key)
            if im is None:
                im = cv2.imread(os.path.join(full_dir, f'{k:04d}_z{zi}.jpg'))
                with zlock:
                    if len(zcache) > 30: zcache.clear()
                    zcache[key] = im
            return im
        pz = pano.copy()
        with ThreadPoolExecutor(TILE_WORKERS) as ex:
            for r in ex.map(lambda t: compose_tile(t[0], t[1], load_z, only=zset), _tiles):
                if r is None: continue
                x0, y0, sub, subc = r
                region = pz[y0:y0 + sub.shape[0], x0:x0 + sub.shape[1]]
                region[subc] = sub[subc]
        cv2.imwrite(os.path.join(out, f'pano_z{z}.jpg'), pz, [cv2.IMWRITE_JPEG_QUALITY, 92])
        del pz
    log(f'z-stack : {ZL} plans sur {len(zfields)} champ(s) en {time.time() - t_z:.0f} s')
else:
    ZL = 0

fields, placements = [], {}
med_sharp = float(np.median([sel[k]['sharpn'] for k in comp]))
for k in comp:
    mk = cv2.imread(os.path.join(full_dir, f'{k:04d}_mask.png'), 0)
    cnts, _ = cv2.findContours((mk > 0).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if cnts:
        hull = cv2.convexHull(max(cnts, key=cv2.contourArea)); poly = cv2.approxPolyDP(hull, 0.004 * cv2.arcLength(hull, True), True)[:, 0, :].astype(np.float32)
    else:
        h, w = sizes[k]; poly = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    pts = cv2.transform(poly[None], M[k])[0]
    fields.append(dict(id=f'{k:04d}', pts=[[int(round(x)), int(round(y))] for x, y in pts], scale=round(1 / scale_k[k], 3),
                       sharp=round(sel[k]['sharpn'] / max(med_sharp, 1e-9), 2), z=bool(sel[k].get('planes')), tracked=(k not in linked)))
    placements[str(k)] = M[k].tolist()
json.dump(fields, open(os.path.join(out, 'fields.json'), 'w'))
um_px = (float(meta['um_per_px']) * smin / (scales[ref] * shrink)) if meta.get('um_per_px') else None   # µm par pixel de la mosaïque

# --- rapport de qualite : trous, nettete, recalage, images rejetees ---
SQ = 8
cov_s = cv2.resize(covered, (max(1, W // SQ), max(1, H // SQ)), interpolation=cv2.INTER_AREA) > 127
pts_s = np.argwhere(cov_s)[:, ::-1].astype(np.int32)
holes, holes_frac = [], 0.0
if len(pts_s) > 10:
    # trou = zone non couverte entierement entouree de couverture (les echancrures du contour n'en sont pas)
    pad = np.pad(~cov_s, 1, constant_values=True).astype(np.uint8)
    n_, lab_, st_, _ = cv2.connectedComponentsWithStats(pad, connectivity=4)
    outside = set(np.unique(np.concatenate([lab_[0], lab_[-1], lab_[:, 0], lab_[:, -1]])))
    field_area = float(np.median([cv2.contourArea(np.float32(f['pts'])) for f in fields])) / (SQ * SQ)
    gap_px = 0
    for c in range(1, n_):
        if c in outside or st_[c, cv2.CC_STAT_AREA] < max(4, 0.01 * field_area): continue
        gap_px += int(st_[c, cv2.CC_STAT_AREA])
        cnts, _ = cv2.findContours((lab_ == c).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cn in cnts:
            cn = cv2.approxPolyDP(cn, 1.5, True)
            holes.append([[int((x - 1) * SQ), int((y - 1) * SQ)] for x, y in cn[:, 0, :]])
    holes_frac = float(gap_px / max(1, cov_s.sum()))
low_frac = float(np.mean([f['sharp'] < 0.5 for f in fields]))
tracked_only = int(sum(1 for f in fields if f['tracked']))
gain_spread = max(float(np.abs(np.log(v)).max()) for v in gains.values()) * 100 if gains else 0.0
res_med, res_max = float(np.median(res)), float(np.max(res))
cov_px = int((covered > 0).sum())
notes = []
score = 0
if res_med > 3.0: notes.append(f'recalage imprécis ({res_med:.1f} px)'); score += 2
elif res_med > 1.5: notes.append(f'recalage moyen ({res_med:.1f} px)'); score += 1
if holes_frac > 0.08 or len(holes) >= 4: notes.append(f'{len(holes)} trou(s) dans la couverture ({holes_frac * 100:.0f} %)'); score += 2
elif holes: notes.append(f'{len(holes)} trou(s) dans la couverture'); score += 1
if low_frac > 0.25: notes.append(f'{low_frac * 100:.0f} % de champs peu nets'); score += 2
elif low_frac > 0.10: notes.append(f'{low_frac * 100:.0f} % de champs peu nets'); score += 1
if tracked_only: notes.append(f'{tracked_only} champ(s) placé(s) sans recalage fin'); score += 1
if n_blurred > 0.5 * n_candidates: notes.append(f'{n_blurred} images floues rejetées sur {n_candidates}'); score += 1
grade = 'A' if score == 0 else 'B' if score <= 2 else 'C'
quality = dict(grade=grade, notes=notes, frames=dict(candidates=n_candidates, blurred=n_blurred, selected=N, used=len(comp), tracked_only=tracked_only),
               residual_px=round(res_med, 2), residual_max_px=round(res_max, 2), holes=holes, holes_frac=round(holes_frac, 4),
               coverage_px=cov_px, coverage_mm2=(round(cov_px * um_px * um_px / 1e6, 3) if um_px else None),
               low_sharp_frac=round(low_frac, 3), gain_spread_pct=round(gain_spread, 1), flat_field='mesuré' if flat is not None else 'estimé',
               k1=round(k1, 4), zstack_fields=len(zfields), zstack_levels=ZL)
json.dump(quality, open(os.path.join(out, 'quality.json'), 'w'), indent=1)
log(f'qualité : note {grade}' + (' (' + ', '.join(notes) + ')' if notes else ' (aucune remarque)'))

json.dump(dict(size=[W, H], images=len(comp), of=N, placements=placements, k1=k1, chroma=chroma,
               flat_field='mesuré' if flat is not None else 'estimé', residual_px=res_med,
               um_per_px=um_px, zstack=ZL, quality=quality,
               title=meta.get('title'), stain=meta.get('stain'), objective=meta.get('objective'), sample=meta.get('sample'),
               comment=meta.get('comment'), parent=meta.get('parent'), captured=meta.get('captured'),
               series=meta.get('series'), component=a.component, other_components=other_components,
               segment=a.segment, segments=len(segs), zone_components=zone_ids, sources=[sel[k]['path'] for k in comp]),
          open(os.path.join(out, 'layout.json'), 'w'), indent=1)
log('terminé : pano.jpg', W, 'x', H, ',', len(comp), 'images utilisées sur', N)
