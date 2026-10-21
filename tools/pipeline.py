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
ap.add_argument('--allow-tracked', action='store_true', help='composer meme si la plupart des champs ne se recalent que par le suivi')
ap.add_argument('--no-seams', action='store_true', help='coutures a mi-chemin (lignes droites) au lieu des coutures qui suivent le fond')
ap.add_argument('--depth', choices=['auto', 'off', 'on'], default='auto', help='piles de profondeur (EDF + plans) : off = image la plus nette a chaque arret ; auto = selon serie.json (depth), off par defaut')
ap.add_argument('--segment', type=int, default=0, help='segment d objectif de la prise (0 = premier) ; voir meta.segments')
ap.add_argument('--parent', default=None, help='lame d ensemble (slug dans lames/, ou dossier result d un segment) : les photos y sont ancrees pendant l assemblage')
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
    """(variance du laplacien, mesure normalisee par le contenu : energie haute frequence / energie du gradient, energie du gradient)"""
    lap = cv2.Laplacian(g, cv2.CV_64F); gx = cv2.Sobel(g, cv2.CV_64F, 1, 0); gy = cv2.Sobel(g, cv2.CV_64F, 0, 1)
    content = gx.var() + gy.var()
    return float(lap.var()), float(lap.var() / (content + 1e-6)), float(content)
sharps, sharpn, contents, smalls = [], [], [], []
orig_paths = [p for p, _ in items]
for p, t in items:
    g = cv2.imread(p, cv2.IMREAD_REDUCED_GRAYSCALE_4)
    sv, sn, ct = sharpness(g); sharps.append(sv); sharpn.append(sn); contents.append(ct)
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
# --- planche-contact : 24 images reparties sur la prise, avec leur nettete, pour voir ce que la camera a filme ---
try:
    idxs = [int(i) for i in np.linspace(0, len(items) - 1, min(24, len(items)))]
    thumbs = []
    for i in idxs:
        th = cv2.imread(items[i][0], cv2.IMREAD_REDUCED_COLOR_8)
        th = cv2.resize(th, (300, int(300 * th.shape[0] / th.shape[1])))
        cv2.putText(th, f'{items[i][1] / 1000:.0f}s  net {sharps[i]:.0f}', (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3)
        cv2.putText(th, f'{items[i][1] / 1000:.0f}s  net {sharps[i]:.0f}', (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        thumbs.append(th)
    hmax = max(t.shape[0] for t in thumbs)
    thumbs = [cv2.copyMakeBorder(t, 0, hmax - t.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=(40, 40, 40)) for t in thumbs]
    rows = [np.hstack(thumbs[r:r + 6] + [np.full((hmax, 300, 3), 40, np.uint8)] * (6 - len(thumbs[r:r + 6]))) for r in range(0, len(thumbs), 6)]
    cv2.imwrite(os.path.join(out, 'planche.jpg'), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 82])
except Exception as e_:
    log('planche-contact impossible : ' + str(e_))
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
# un champ presque vide a un laplacien faible sans etre flou : il n'est rejete que si sa nettete normalisee (independante du contenu) est basse aussi
med_sn = float(np.median(sharpn)); med_ct = float(np.median(contents))
content_of = dict(zip(orig_paths, contents))
def is_blurred(p, sharp):
    if sharp >= thr: return False
    empty = content_of.get(p, med_ct) < 0.25 * med_ct
    return (not empty) or sharpn_of.get(p, med_sn) < 0.5 * med_sn
log(f'netteté médiane {np.median(sharps):.1f}, seuil {thr:.1f}')
sel = []
last_pos = None
n_dark = 0
RETAKE = {f.get('name'): bool(f.get('retake') or f.get('bonus')) for f in (meta.get('frames') or []) if f.get('name')}   # reprises de mise au point et photos bonus (mode renforcer) : la plus nette remplace au meme endroit
for (p, t), sharp in zip(items, sharps):
    if is_blurred(p, sharp):
        continue
    g8 = cv2.imread(p, cv2.IMREAD_REDUCED_GRAYSCALE_8)
    if g8 is not None and float(np.percentile(g8, 90)) < 70:   # image sombre : telephone hors de l'oculaire, pas un champ
        n_dark += 1; continue
    pos = pos_at(t)
    planes = planes_of.get(p)
    if pos and last_pos and W0 and math.hypot(pos[0] - last_pos[0], pos[1] - last_pos[1]) < 0.15 * max(W0, H0):
        if planes and sel and not sel[-1].get('planes'): sel.pop()     # une pile de profondeur remplace l'image simple prise au meme endroit
        elif RETAKE.get(os.path.basename(p)) and sel and not sel[-1].get('planes') and sharp > sel[-1]['sharp']: sel.pop()   # reprise de mise au point : la plus nette remplace
        else: continue
    if pos: last_pos = pos
    sel.append(dict(path=p, t=t, sharp=sharp, sharpn=sharpn_of[p], pos=pos, planes=planes))
n_blurred = sum(1 for (p, _), sh in zip(items, sharps) if is_blurred(p, sh))
if len(sel) > a.max_frames:
    step = len(sel) / a.max_frames
    sel = [sel[int(i * step)] for i in range(a.max_frames)]
if n_dark: log(f'{n_dark} image(s) sombre(s) écartée(s) (téléphone hors de l\'oculaire)')
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
flat_kind = 'mesuré' if flat is not None else None
if flat is None and len(sel) >= 8:
    # mediane temporelle des images retenues : ce qui ne bouge pas avec la platine (vignettage, poussieres de
    # l'oculaire, taches du capteur) reste, le specimen qui defile disparait. Calculee au 1/4, legerement lissee.
    pick = [sel[int(i)] for i in np.linspace(0, len(sel) - 1, min(40, len(sel)))]
    stack, shape0 = [], None
    for it in pick:
        im4 = cv2.imread(it['path'], cv2.IMREAD_REDUCED_COLOR_4)
        if im4 is None: continue
        if shape0 is None: shape0 = im4.shape
        if im4.shape == shape0: stack.append(im4.astype(np.float32))
    if len(stack) >= 8:
        med = np.median(np.stack(stack), axis=0)
        m4 = disc_mask(np.clip(med, 0, 255).astype(np.uint8)) > 0
        inside = med[m4].mean(axis=-1) if m4.any() else med.mean(axis=-1).ravel()
        if inside.mean() > 110 and inside.std() / max(inside.mean(), 1) < 0.35:
            med = cv2.GaussianBlur(med, (0, 0), 2)
            full0 = cv2.imread(pick[0]['path'])
            flat = cv2.resize(med, (full0.shape[1], full0.shape[0]), interpolation=cv2.INTER_LINEAR)
            flat_kind = 'médiane'
            log(f'flat-field : médiane temporelle de {len(stack)} images (vignettage et poussières fixes de l\'oculaire)')
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

# --- verification des paires SIFT par correlation sur le recouvrement (frottis : cellules toutes semblables -> fausses paires en reseau) ---
def _gray_clahe(reg):
    return cv2.createCLAHE(3.0, (8, 8)).apply(cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY)).astype(np.float32)
GRAY = {}
def gray_of(k):
    if k not in GRAY: GRAY[k] = _gray_clahe(regs[k])
    return GRAY[k]
def overlap_cc(i, j, A):
    """correlation normalisee entre i et j ramenee dans i par A (similitude j -> i) ; None si recouvrement trop faible"""
    gi = gray_of(i); h_, w_ = gi.shape
    gj = cv2.warpAffine(gray_of(j), A, (w_, h_), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=-1e4)
    mj = cv2.warpAffine(rmasks[j], A, (w_, h_), flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    m = (gj > -1e3) & (rmasks[i] > 0) & (mj > 0)
    if m.sum() < 0.08 * w_ * h_: return None
    x, y = gi[m], gj[m]; x = x - x.mean(); y = y - y.mean()
    den = math.sqrt(float((x * x).sum()) * float((y * y).sum()))
    return float((x * y).sum() / den) if den > 0 else 0.0
_rej = []
for ij, (pi, pj, A, n) in list(pairs.items()):
    cc_ = overlap_cc(ij[0], ij[1], A)
    if cc_ is not None and cc_ < 0.4: _rej.append((ij, cc_))
for ij, _ in _rej: del pairs[ij]
if _rej: log(f'{len(_rej)} paire(s) SIFT rejetée(s) : les images ne se superposent pas à l\'endroit trouvé (motif répétitif)')

# --- recalage par correlation (frottis sanguins, champs repetitifs ou pauvres en points SIFT) ---
# pour chaque paire candidate sans paire SIFT : plusieurs gabarits de j cherches dans toute l'image i (demi-echelle),
# accepte si un pic net et non ambigu, ou si deux gabarits s'accordent ; puis affinage pleine echelle et ECC sous-pixel
HALF = {}
def half_of(k):
    if k not in HALF: HALF[k] = cv2.resize(gray_of(k), None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    return HALF[k]
def tm_pair(ij):
    i, j = ij
    if (i, j) in pairs: return None
    gi2, gj2 = half_of(i), half_of(j); h2, w2 = gi2.shape; hr_, wr_ = gray_of(i).shape
    T = min(128, min(h2, w2) // 3)
    spots = [(int(fx_ * w2 - T / 2), int(fy_ * h2 - T / 2), T) for fy_ in (0.17, 0.5, 0.83) for fx_ in (0.25, 0.75)]
    # voisins a faible recouvrement (colonnes espacees) : gabarits plus petits le long du bord de j qui regarde i,
    # d'apres la direction du suivi (sa norme est approximative, sa direction fiable)
    dxp = dyp = 0.0
    if P[i] and P[j]:
        dxp, dyp = (P[j][0] - P[i][0]) * wr_ / W0, (P[j][1] - P[i][1]) * wr_ / W0
        Tb = 48
        if abs(dxp) > 0.45 * wr_:
            tx = 4 if dxp > 0 else w2 - Tb - 4
            spots += [(tx, int(fy_ * (h2 - Tb)), Tb) for fy_ in (0.05, 0.23, 0.41, 0.59, 0.77, 0.95)]
        if abs(dyp) > 0.45 * hr_:
            ty = 4 if dyp > 0 else h2 - Tb - 4
            spots += [(int(fx_ * (w2 - Tb)), ty, Tb) for fx_ in (0.05, 0.23, 0.41, 0.59, 0.77, 0.95)]
    hits = []
    for tx, ty, Tk in spots:
        tmpl = gj2[ty:ty + Tk, tx:tx + Tk]
        if tmpl.std() < 2.0: continue                     # gabarit uniforme (fond vide) : inutilisable
        res = cv2.matchTemplate(gi2, tmpl, cv2.TM_CCOEFF_NORMED)
        _, cc, _, loc = cv2.minMaxLoc(res)
        res2 = res.copy(); cv2.circle(res2, loc, 8, -1, -1); _, cc2, _, _ = cv2.minMaxLoc(res2)
        hits.append((cc, cc - cc2, loc[0] - tx, loc[1] - ty))
    if not hits: return None
    best = max(hits)
    agree = [hh for hh in hits if hh[0] >= 0.6 and math.hypot(hh[2] - best[2], hh[3] - best[3]) < 3]
    ok = (best[0] >= 0.75 and best[1] >= 0.2) or (len(agree) >= 2 and best[1] >= 0.1)
    weak = False
    if not ok and P[i] and P[j]:
        # pic plus faible (champ pale) mais coherent avec le deplacement mesure par le suivi : accepte avec un poids reduit
        near = [hh for hh in hits if hh[0] >= 0.45 and math.hypot(2 * hh[2] - dxp, 2 * hh[3] - dyp) < 0.15 * max(wr_, hr_)]
        if near:
            best = max(near); agree = [hh for hh in near if math.hypot(hh[2] - best[2], hh[3] - best[3]) < 3]; ok = weak = True
    if not ok: return None
    dx, dy = 2.0 * best[2], 2.0 * best[3]
    thin = 0.06 if (len(agree) >= 3 or weak) else 0.12
    if wr_ - abs(dx) < thin * wr_ or hr_ - abs(dy) < thin * hr_: return None
    # affinage pleine echelle : gabarit au centre du recouvrement, recherche locale +-12 px
    gi, gj = gray_of(i), gray_of(j)
    x0_, y0_ = int(max(0, -dx)), int(max(0, -dy)); x1_, y1_ = int(min(wr_, wr_ - dx)), int(min(hr_, hr_ - dy))
    tw_, th_ = min(x1_ - x0_ - 20, 300), min(y1_ - y0_ - 20, 300)
    if tw_ >= 40 and th_ >= 40:
        tx0, ty0 = (x0_ + x1_) // 2 - tw_ // 2, (y0_ + y1_) // 2 - th_ // 2
        sx0, sy0 = max(0, int(tx0 + dx - 12)), max(0, int(ty0 + dy - 12)); sx1, sy1 = min(wr_, int(tx0 + dx + tw_ + 12)), min(hr_, int(ty0 + dy + th_ + 12))
        win = gi[sy0:sy1, sx0:sx1]
        if win.shape[0] > th_ and win.shape[1] > tw_:
            res = cv2.matchTemplate(win, gj[ty0:ty0 + th_, tx0:tx0 + tw_], cv2.TM_CCOEFF_NORMED)
            _, cc, _, loc = cv2.minMaxLoc(res)
            if cc >= 0.5: dx, dy = float(sx0 + loc[0] - tx0), float(sy0 + loc[1] - ty0)
    try:
        W_ = np.float32([[1, 0, -dx], [0, 1, -dy]])
        _, W_ = cv2.findTransformECC(gi, gj, W_, cv2.MOTION_TRANSLATION, (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 1e-3), None, 3)
        if math.hypot(-W_[0, 2] - dx, -W_[1, 2] - dy) < 3: dx, dy = float(-W_[0, 2]), float(-W_[1, 2])
    except cv2.error:
        pass
    A_ = np.float32([[1, 0, dx], [0, 1, dy]])
    cc_ = overlap_cc(i, j, A_)
    if os.environ.get('SCANLAME_DUMP'): log(f'  corr {i}-{j} : {len(agree)} gabarit(s) d\'accord, pic {best[0]:.2f}, recouvrement cc {cc_}')
    if cc_ is None and (weak or len(agree) < 3): return None            # recouvrement trop mince pour verifier
    if cc_ is not None and cc_ < (0.25 if len(agree) >= 3 else 0.4): return None
    ox0, oy0 = max(0, dx), max(0, dy); ox1, oy1 = min(wr_, wr_ + dx), min(hr_, hr_ + dy)
    xs = np.linspace(ox0 + 10, ox1 - 10, 8); ys = np.linspace(oy0 + 10, oy1 - 10, 6)
    pi_ = np.float32([[x, y] for y in ys for x in xs]); pj_ = pi_ - np.float32([dx, dy])
    return (i, j), (pi_, pj_, A_, 30 if weak else max(a.min_inliers, int(60 * best[0])))
todo = [ij for ij in sorted(cand) if ij not in pairs]
if os.environ.get('SCANLAME_DUMP'):
    import pickle
    pickle.dump(dict(regs=regs, rmasks=rmasks, P=P, cand=sorted(cand), pairs=pairs, scales=scales, W0=W0, H0=H0), open(os.path.join(out, 'dump.pkl'), 'wb'))
if todo:
    with ThreadPoolExecutor(NWORK) as ex:
        added = [r_ for r_ in ex.map(tm_pair, todo) if r_ is not None]
    for ij, val in added: pairs[ij] = val
    if added: log(f'{len(added)} paire(s) recalée(s) par corrélation d\'images (champs répétitifs ou pauvres en points)')
del GRAY, HALF
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

# --- ancrage dans la lame d'ensemble (--parent) : groupes de photos consecutives apparies dans la lame parente ---
# chaque groupe bien apparie donne des contraintes absolues (ou est chaque photo dans le parent) : la derive accumulee
# le long d'une bande (courbure) est corrigee, et la transformation enfant -> parent sort directement de l'ajustement.
anchor_corr = []          # (k, p px recalage image k, q px pleine resolution parent)
groups_failed = []
G_init = None             # similitude parent pleine res -> repere de recalage (initialisation)
anchored_imgs = set()
if a.parent:
  try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import link_child as LC
    if os.path.isdir(a.parent):
        plj = json.load(open(os.path.join(a.parent, 'layout.json')))
        pim = cv2.imread(os.path.join(a.parent, 'pano.jpg'))
        pj = dict(width=plj['size'][0], height=plj['size'][1], objective=plj.get('objective'), um_per_px=plj.get('um_per_px'))
        sp_ = min(1.0, 8000 / max(pim.shape[:2]))
        if sp_ < 1: pim = cv2.resize(pim, None, fx=sp_, fy=sp_, interpolation=cv2.INTER_AREA)
    else:
        pj = LC.load_lame(a.parent)
        if not pj: raise RuntimeError('lame parente introuvable')
        pim, sp_ = LC.reg_image(a.parent, pj, max_side=8000)
    order = sorted(comp)
    # 1) mosaique provisoire (chainage des paires) a basse resolution, pour localiser grossierement la prise dans le parent
    c4 = np.vstack([cv2.transform(np.float32([[0, 0], [regs[k].shape[1], 0], [regs[k].shape[1], regs[k].shape[0]], [0, regs[k].shape[0]]])[None], T[k][:2])[0] for k in comp])
    mn_, mx_ = c4.min(0), c4.max(0)
    po, co = LC.objective_number(pj.get('objective')), LC.objective_number(meta.get('objective'))
    cands = [po / co] if (po and co) else [0.1, 0.16, 0.25, 0.4, 0.63, 1.0]       # px enfant pleine res -> px parent pleine res
    # mosaique provisoire rendue directement a l'echelle attendue du parent reduit (facteur ~1 pour le 1er candidat), 4000 px au plus
    prov_s = min(cands[0] * sp_ / scales[ref], 4000 / max(mx_ - mn_))
    Sp = np.array([[prov_s, 0, -mn_[0] * prov_s], [0, prov_s, -mn_[1] * prov_s], [0, 0, 1]])
    pw, ph = int(math.ceil((mx_ - mn_)[0] * prov_s)) + 1, int(math.ceil((mx_ - mn_)[1] * prov_s)) + 1
    prov = np.full((ph, pw, 3), 255, np.uint8)
    for k in order:
        Mk = (Sp @ T[k])[:2]
        w_ = cv2.warpAffine(regs[k], Mk, (pw, ph)); m_ = cv2.warpAffine(rmasks[k], Mk, (pw, ph), flags=cv2.INTER_NEAREST) > 0
        prov[m_] = w_[m_]
    f_c = [r_ * sp_ / (scales[ref] * prov_s) for r_ in cands]                       # px mosaique provisoire -> px parent reduit
    prior_box = None
    hint, stage = meta.get('hint'), meta.get('stage')
    seg_from = (segs[a.segment] if segs and a.segment < len(segs) else {}) if 'segs' in dir() else {}
    parent_after = os.path.isdir(a.parent) and segs and plj.get('segment') is not None and plj['segment'] < len(segs) and (segs[plj['segment']].get('t0') or 0) > (segs[a.segment].get('t0') or 0)
    pfields = None; anchor_last = None
    if parent_after:
        try: pfields = [x for x in json.load(open(os.path.join(a.parent, 'fields.json'))) if x.get('pts')]
        except Exception: pfields = None
    if parent_after and pfields:
        # la lame d'ensemble a ete enregistree APRES le detail : la platine n'a pas bouge au changement d'objectif,
        # donc la derniere photo du detail est au centre du premier champ de l'ensemble : a priori exact
        p0 = np.array(min(pfields, key=lambda e: e['id'])['pts'], float).mean(axis=0)
        anchor_last = p0
        hw = pw * f_c[0] * 1.1 + 0.05 * pim.shape[1]; hh = ph * f_c[0] * 1.1 + 0.05 * pim.shape[0]
        prior_box = (p0[0] * sp_ - hw, p0[1] * sp_ - hh, p0[0] * sp_ + hw, p0[1] * sp_ + hh)
        log(f'a priori (ensemble enregistré après le détail) : dernière photo du détail au centre du premier champ de l\'ensemble ({p0[0]:.0f}, {p0[1]:.0f})')
    elif seg_from.get('from_x') is not None and os.path.isdir(a.parent) and plj.get('track_to_mosaic'):
        # position de la platine au clic « changer d'objectif », convertie en px de la lame d'ensemble (segment 0 de la meme prise)
        fx_, fy_ = cv2.transform(np.float32([[seg_from['from_x'], seg_from['from_y']]])[None], np.float32(plj['track_to_mosaic']))[0][0]
        hw = max(pw * f_c[0], 0.2 * pim.shape[1]); hh = max(ph * f_c[0], 0.2 * pim.shape[0])
        prior_box = (fx_ * sp_ - hw, fy_ * sp_ - hh, fx_ * sp_ + hw, fy_ * sp_ + hh)
        log(f'a priori (position au changement d\'objectif, suivi {seg_from.get("from_track")}) : premier champ attendu vers ({fx_:.0f}, {fy_:.0f}) px de la lame d\'ensemble')
    elif hint or stage:
        pj['_slug'] = a.parent if not os.path.isdir(a.parent) else None
        try: prior, pk = LC.prior_center(pj, dict(hint=hint, stage=stage)) if pj.get('_slug') else ((np.array([hint['fx'] * pj['width'], hint['fy'] * pj['height']]), 'repère') if hint else (None, None))
        except Exception: prior, pk = None, None
        if prior is not None:
            hw = max(pw * f_c[0], 0.15 * pim.shape[1]); hh = max(ph * f_c[0], 0.15 * pim.shape[0])
            prior_box = (prior[0] * sp_ - hw, prior[1] * sp_ - hh, prior[0] * sp_ + hw, prior[1] * sp_ + hh)
            log(f'a priori ({pk}) : prise attendue vers ({prior[0]:.0f}, {prior[1]:.0f}) px du parent')
    cv2.imwrite(os.path.join(out, 'prov.jpg'), prov, [cv2.IMWRITE_JPEG_QUALITY, 90])   # mosaique provisoire (diagnostic)
    log(f'ancrage dans la lame d\'ensemble {a.parent} ({pj["width"]}x{pj["height"]} px, objectif {pj.get("objective")}) : mosaïque provisoire {pw}x{ph}')
    best = LC.match_into_parent(prov, pim, f_c, prior_box, log=log)
    if not best: raise RuntimeError('prise non localisée dans la lame d\'ensemble')
    A0, n0, f0 = best[:3]; pts0 = best[3] if len(best) > 3 else None
    G0 = A3(A0) @ np.diag([f0, f0, 1]) @ Sp                 # px recalage (repere ref) -> px parent reduit
    G_init = np.linalg.inv(G0) @ np.diag([sp_, sp_, 1])     # px parent pleine res -> px recalage
    Tinv = {k: np.linalg.inv(T[k]) for k in comp}
    def owner(pr):
        # image de la prise qui contient ce point du repere de recalage (la plus centree), et le point dans ses coordonnees
        bestk = None
        for k in comp:
            q = Tinv[k][:2] @ [pr[0], pr[1], 1]
            h_, w_ = rmasks[k].shape
            if 0 <= q[0] < w_ and 0 <= q[1] < h_ and rmasks[k][int(q[1]), int(q[0])] > 0:
                d = min(q[0], w_ - q[0], q[1], h_ - q[1])
                if bestk is None or d > bestk[0]: bestk = (d, k, q)
        return (bestk[1], bestk[2]) if bestk else (None, None)
    # 2) les points communs de la localisation grossiere sont deja des ancrages (epars, la ou la lame a du contenu)
    if pts0 is not None:
        Spinv = np.linalg.inv(Sp)
        for s_pt, d_pt in zip(*pts0):
            pr = Spinv[:2] @ [s_pt[0] / f0, s_pt[1] / f0, 1]
            k, q = owner(pr)
            if k is None: continue
            anchor_corr.append((k, q, d_pt / sp_)); anchored_imgs.add(k)
        log(f'ancrage global : {len(anchor_corr)} points sur {len(anchored_imgs)} images')
    if parent_after and pfields and anchor_last is not None:
        # ancrage de la derniere photo du detail sur le premier champ de l'ensemble : centre sur centre, orientation donnee par la
        # localisation grossiere (G0) ; poids via 4 points autour du centre
        kl = max(comp); h_, w_ = rmasks[kl].shape
        Mk = (G0 @ T[kl])[:2]; c_pim = cv2.transform(np.float32([[w_ / 2, h_ / 2]])[None], Mk)[0][0]
        shift = anchor_last * sp_ - c_pim        # decalage a appliquer (px parent reduit) pour amener le centre au bon endroit
        for dx, dy in ((-0.25, -0.25), (0.25, -0.25), (0.25, 0.25), (-0.25, 0.25)):
            p_ = np.array([w_ * (0.5 + dx), h_ * (0.5 + dy)])
            q_ = (cv2.transform(np.float32([p_])[None], Mk)[0][0] + shift) / sp_
            anchor_corr.append((kl, p_, q_))
        anchored_imgs.add(kl)
        log(f'  dernière photo ({kl:04d}) ancrée au centre du premier champ de l\'ensemble (écart à la localisation grossière : {np.hypot(*shift) / sp_:.0f} px parent)')
    log(f'localisation grossière : {n0} inliers, échelle enfant->parent {math.hypot(G0[0, 0], G0[1, 0]) * scales[ref] / sp_:.4f}')
    dbg = os.path.join(out, 'ancrage'); os.makedirs(dbg, exist_ok=True)
    match = LC.learned_matcher()
    if match is None: raise RuntimeError('appariement appris indisponible : pas d\'ancrage par groupes')
    _cl = cv2.createCLAHE(2.0, (8, 8))
    def clahe3(im):
        l = cv2.cvtColor(im, cv2.COLOR_BGR2LAB); l[:, :, 0] = _cl.apply(l[:, :, 0]); return cv2.cvtColor(l, cv2.COLOR_LAB2BGR)
    def anchor_groups(groups, Tcur, Gcur, margin_frac, tag):
        """Apparie chaque groupe de photos (rendu dans le repere du parent d'apres Tcur et Gcur) dans une fenetre du parent ; ajoute les ancrages."""
        ok_g = 0; done = []
        for g in groups:
            Mg = {k: (Gcur @ Tcur[k])[:2] for k in g}
            cg = np.vstack([cv2.transform(np.float32([[0, 0], [regs[k].shape[1], 0], [regs[k].shape[1], regs[k].shape[0]], [0, regs[k].shape[0]]])[None], Mg[k])[0] for k in g])
            o_ = np.floor(cg.min(0)); cw, ch = int(math.ceil(cg.max(0)[0] - o_[0])) + 1, int(math.ceil(cg.max(0)[1] - o_[1])) + 1
            if cw < 40 or ch < 40 or cw * ch > 6e6: continue
            canvas = np.full((ch, cw, 3), 255, np.uint8)
            for k in g:
                Mk = Mg[k].copy(); Mk[:, 2] -= o_
                w_ = cv2.warpAffine(regs[k], Mk, (cw, ch)); m_ = cv2.warpAffine(rmasks[k], Mk, (cw, ch), flags=cv2.INTER_NEAREST) > 0
                canvas[m_] = w_[m_]
            margin = int(margin_frac * max(cw, ch)) + 60
            x0_, y0_ = int(max(0, o_[0] - margin)), int(max(0, o_[1] - margin)); x1_, y1_ = int(min(pim.shape[1], o_[0] + cw + margin)), int(min(pim.shape[0], o_[1] + ch + margin))
            if x1_ - x0_ < 60 or y1_ - y0_ < 60: continue
            win = pim[y0_:y1_, x0_:x1_]
            cv2.imwrite(os.path.join(dbg, f'{tag}{g[0]:04d}_canvas.jpg'), canvas); cv2.imwrite(os.path.join(dbg, f'{tag}{g[0]:04d}_window.jpg'), win)
            src, dst = match(clahe3(canvas), clahe3(win)); dst = dst + [x0_, y0_]      # contraste egalise : le fond du detail est souvent plus clair que le parent
            Ag, ng, sg = LC.fit(src, dst, thr=4.0)
            rot = math.degrees(math.atan2(Ag[1, 0], Ag[0, 0])) if Ag is not None else 0
            shift = np.hypot(*(cv2.transform(np.float32([[cw / 2, ch / 2]])[None], Ag)[0][0] - o_ - [cw / 2, ch / 2])) if Ag is not None else 0
            good = Ag is not None and ng >= 25 and 0.9 < sg < 1.1 and (abs(rot) < 6 or (ng >= 200 and abs(rot) < 15))   # beaucoup de points : une rotation forte (debut d'arc) est credible
            log(f'  groupe {g[0]:04d}-{g[-1]:04d} : {len(src)} appariements, {ng} inliers, échelle {sg:.3f}, rotation {rot:+.1f}°, décalage {shift:.0f} px parent' + ('' if good else ' : rejeté'))
            if not good: continue
            ok_g += 1; done.append(g)
            for k in g:
                h_, w_ = rmasks[k].shape
                ys, xs = np.mgrid[h_ * 0.1:h_ * 0.9:6j, w_ * 0.1:w_ * 0.9:6j]
                pts = np.c_[xs.ravel(), ys.ravel()]
                pts = pts[rmasks[k][pts[:, 1].astype(int), pts[:, 0].astype(int)] > 0]
                if len(pts) < 4: continue
                qc = cv2.transform(np.float32(pts)[None], Mg[k])[0] - o_          # px canvas
                qp = cv2.transform(np.float32(qc)[None], Ag)[0] / sp_               # px parent pleine res
                anchor_corr.extend((k, p_, q_) for p_, q_ in zip(pts, qp)); anchored_imgs.add(k)
        return ok_g, done
    GS = 5
    groups = [order[i:i + GS] for i in range(0, len(order), GS)]
    if len(groups) > 1 and len(groups[-1]) < 3: groups[-2] += groups.pop()
    ok_g, done = anchor_groups(groups, T, G0, 0.3, 'g')
    groups_failed = [g for g in groups if g not in done]
    log(f'ancrage : {ok_g}/{len(groups)} groupes appariés, {len(anchored_imgs)} images ancrées, {len(anchor_corr)} points')
    if not anchor_corr: G_init = G_init   # localisation seule : la transformation d'ensemble est quand meme conservee
  except Exception as e:
    log(f'ancrage dans la lame d\'ensemble impossible : {e}')
    anchor_corr = []; G_init = None

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
    n_pair_eq = eq
    # ancrages : T_k(p) - G(q) = 0, G (4 params, parent pleine res -> repere de recalage) inconnue commune
    nG = 4 if (anchor_corr and G_init is not None) else 0
    base_g = 4 * len(idx)
    for (k, p, q) in anchor_corr:
        r1 = add(eq, k, float(p[0]), float(p[1]), 1.0)
        rows.extend([eq, eq, eq, eq + 1, eq + 1, eq + 1]); cols.extend([base_g, base_g + 1, base_g + 2, base_g, base_g + 1, base_g + 3])
        vals.extend([-float(q[0]), float(q[1]), -1.0, -float(q[1]), -float(q[0]), -1.0])
        rhs.extend([r1[0], r1[1]]); eq += 2
    Amat = sp.csr_matrix((vals, (rows, cols)), shape=(eq, 4 * len(idx) + nG))
    bvec = np.array(rhs)
    x0 = np.zeros(4 * len(idx) + nG)
    for k, n in idx.items():
        x0[4 * n:4 * n + 4] = [T[k][0, 0], T[k][1, 0], T[k][0, 2], T[k][1, 2]]
    if nG: x0[base_g:base_g + 4] = [G_init[0, 0], G_init[1, 0], G_init[0, 2], G_init[1, 2]]
    is_anchor = np.zeros(eq // 2, bool); is_anchor[n_pair_eq // 2:] = True
    W_ANCHOR, DELTA_ANCHOR = 0.5, 8.0    # poids relatif et seuil Huber (px de recalage) des ancrages : ils corrigent la derive sans ecraser le recalage fin
    x = x0.copy(); w = np.where(np.repeat(is_anchor, 2), W_ANCHOR, 1.0)
    for it in range(5):
        # equations normales (quelques centaines a quelques milliers d'inconnues) : solution exacte, pas de probleme de convergence
        Wd = sp.diags(w)
        AtWA = (Amat.T @ Wd @ Amat).toarray(); AtWb = Amat.T @ (w * bvec)
        try: x = np.linalg.solve(AtWA + 1e-9 * np.eye(len(AtWA)), AtWb)
        except np.linalg.LinAlgError: x = spl.lsqr(sp.diags(np.sqrt(w)) @ Amat, np.sqrt(w) * bvec, x0=x, atol=1e-10, btol=1e-10, iter_lim=20000)[0]
        res_v = Amat @ x - bvec
        rp = np.hypot(res_v[0::2], res_v[1::2])
        delta = np.where(is_anchor, DELTA_ANCHOR, 2.0)
        wp = np.where(rp <= delta, 1.0, delta / np.maximum(rp, 1e-6)) * np.where(is_anchor, W_ANCHOR, 1.0)
        w = np.repeat(wp, 2)
    for k, n in idx.items():
        a_, b_, tx, ty = x[4 * n:4 * n + 4]
        T[k] = np.array([[a_, -b_, tx], [b_, a_, ty], [0, 0, 1]])
    sc_ = [math.hypot(T[k][0, 0], T[k][1, 0]) for k in comp]
    log(f'échelles relatives après ajustement : min {min(sc_):.3f}, max {max(sc_):.3f}')
    G_fit = None
    if nG:
        a_, b_, tx, ty = x[base_g:base_g + 4]; G_fit = np.array([[a_, -b_, tx], [b_, a_, ty], [0, 0, 1]])
        ra = rp[is_anchor]
        log(f'ancrages (1re passe) : résidu médian {np.median(ra):.1f} px de recalage, 90e centile {np.percentile(ra, 90):.1f}')
        # 2e passe : les groupes non apparies, fusionnes deux a deux, sont recherches la ou la solution ancree les place (fenetre etroite)
        if groups_failed and os.environ.get('SCANLAME_NO_PASS2') is None:
            merged = []
            i_ = 0
            while i_ < len(groups_failed):
                g = list(groups_failed[i_])
                if i_ + 1 < len(groups_failed) and groups_failed[i_ + 1][0] == g[-1] + 1: g += groups_failed[i_ + 1]; i_ += 1
                merged.append(g); i_ += 1
            Gcur = np.diag([sp_, sp_, 1]) @ np.linalg.inv(G_fit)      # px recalage -> px parent reduit
            n_before = len(anchor_corr)
            ok2, _ = anchor_groups(merged, T, Gcur, 0.15, 'p2_')
            if len(anchor_corr) > n_before:
                log(f'2e passe : {ok2}/{len(merged)} groupes appariés, {len(anchor_corr) - n_before} ancrages de plus ; nouvel ajustement')
                for (k, p, q) in anchor_corr[n_before:]:
                    r1 = add(eq, k, float(p[0]), float(p[1]), 1.0)
                    rows.extend([eq, eq, eq, eq + 1, eq + 1, eq + 1]); cols.extend([base_g, base_g + 1, base_g + 2, base_g, base_g + 1, base_g + 3])
                    vals.extend([-float(q[0]), float(q[1]), -1.0, -float(q[1]), -float(q[0]), -1.0])
                    rhs.extend([r1[0], r1[1]]); eq += 2
                Amat = sp.csr_matrix((vals, (rows, cols)), shape=(eq, 4 * len(idx) + nG)); bvec = np.array(rhs)
                is_anchor = np.zeros(eq // 2, bool); is_anchor[n_pair_eq // 2:] = True
                w = np.where(np.repeat(is_anchor, 2), W_ANCHOR, 1.0)
                for it in range(5):
                    Wd = sp.diags(w); AtWA = (Amat.T @ Wd @ Amat).toarray(); AtWb = Amat.T @ (w * bvec)
                    x = np.linalg.solve(AtWA + 1e-9 * np.eye(len(AtWA)), AtWb)
                    res_v = Amat @ x - bvec; rp = np.hypot(res_v[0::2], res_v[1::2])
                    delta = np.where(is_anchor, DELTA_ANCHOR, 2.0)
                    wp = np.where(rp <= delta, 1.0, delta / np.maximum(rp, 1e-6)) * np.where(is_anchor, W_ANCHOR, 1.0); w = np.repeat(wp, 2)
                for k, n in idx.items():
                    a_, b_, tx, ty = x[4 * n:4 * n + 4]; T[k] = np.array([[a_, -b_, tx], [b_, a_, ty], [0, 0, 1]])
                a_, b_, tx, ty = x[base_g:base_g + 4]; G_fit = np.array([[a_, -b_, tx], [b_, a_, ty], [0, 0, 1]])
        ra = rp[is_anchor]
        log(f'ancrages (final) : résidu médian {np.median(ra):.1f} px de recalage, 90e centile {np.percentile(ra, 90):.1f}')
    elif G_init is not None: G_fit = G_init
    log(f'ajustement global : {len(corr_list)} correspondances, {len(idx)} images' + (f', {len(anchor_corr)} ancrages' if nG else ''))
except ImportError:
    G_fit = G_init
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
extra = 0; extra_single = 0
if anchors and W0:
    src = np.float32([P[k] for k in anchors]); dst = np.float32([(T[k] @ np.array([0, 0, 1]))[:2] for k in anchors])
    Apos = None
    if len(anchors) >= 3:
        Apos, _ = cv2.estimateAffinePartial2D(src, dst, method=cv2.LMEDS)
    if Apos is None:
        s_ = scales[ref]; off = dst.mean(0) - src.mean(0) * s_
        Apos = np.array([[s_, 0, off[0]], [0, s_, off[1]]], np.float64)
    def chain(c):
        # recalage interne d'un fragment : chainage des paires depuis son image la mieux reliee (comme pour la principale)
        r0 = max(c, key=lambda k: sum(adj[k].values())); Tc = {r0: np.eye(3)}
        while len(Tc) < len(c):
            best_ = None
            for k in Tc:
                for v, n in adj[k].items():
                    if v in c and v not in Tc and (best_ is None or n > best_[2]): best_ = (k, v, n)
            if best_ is None: break
            k, v, _ = best_
            Tc[v] = Tc[k] @ (A3(pairs[(k, v)][2]) if (k, v) in pairs else np.linalg.inv(A3(pairs[(v, k)][2])))
        return Tc
    for ci, c in enumerate(comps):
        if ci == a.component or ci in is_zone: continue    # les zones distinctes sont assemblees a part ; le reste est un fragment
        tracked = [k for k in c if P[k]]
        if not tracked: continue
        # Apos convertit les positions du suivi (px du flux) en px de recalage de la reference : seule la
        # translation sert ; l'image garde l'echelle de la reference (toutes les images ont la meme echelle de recalage)
        if len(c) >= 2:
            # fragment recale en interne : place d'un bloc, par la translation mediane entre ses positions suivies et son recalage
            Tc = chain(c)
            offs = []
            for k in tracked:
                if k in Tc:
                    t = (Apos @ np.array([P[k][0], P[k][1], 1.0]))[:2]; o = (Tc[k] @ np.array([0, 0, 1.0]))[:2]
                    offs.append(t - o)
            if offs:
                off = np.median(np.array(offs), axis=0)
                for k in c:
                    if k in Tc:
                        T[k] = np.array([[1.0, 0, off[0]], [0, 1.0, off[1]], [0, 0, 1]]) @ Tc[k]; comp.append(k); extra += 1
                continue
        for k in tracked:
            t = (Apos @ np.array([P[k][0], P[k][1], 1.0]))[:2]
            T[k] = np.array([[1.0, 0, t[0]], [0, 1.0, t[1]], [0, 0, 1]]); comp.append(k); extra += 1; extra_single += 1
    if extra: log(f'{extra} image(s) placée(s) d\'après le suivi en direct ({extra - extra_single} en blocs recalés entre eux, {extra_single} isolée(s))')
    if extra_single > max(3, 0.5 * len(comp)) and not a.allow_tracked:
        log(f'ÉCHEC : {extra_single} champs isolés sur {len(comp)} ne se recalent pas par les points (image floue ou vide ?) ; prise inexploitable, pas de composition')
        sys.exit(3)

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
parent_transform = None
if a.parent and G_fit is not None:
    PT = np.linalg.inv(G_fit) @ np.linalg.inv(Sfin)      # px mosaique finale -> px parent pleine resolution
    parent_transform = [float(PT[0, 0]), float(PT[1, 0]), float(PT[0, 2]), float(PT[1, 2])]
    log(f'position dans la lame d\'ensemble : échelle {math.hypot(PT[0, 0], PT[1, 0]):.4f}, rotation {math.degrees(math.atan2(PT[1, 0], PT[0, 0])):.1f}°, {len(anchored_imgs)} images ancrées')

# par image : matrice canvas<-undistorted (2x3), son inverse, boite englobante, poids (distance au bord, coeur net)
M, Minv, bbox, scale_k, dist_maps = {}, {}, {}, {}, {}
DIST_DS = 4
for k in comp:
    Mk = (Sfin @ Tf[k])
    M[k] = Mk[:2]; Minv[k] = np.linalg.inv(Mk)[:2]
    scale_k[k] = math.hypot(Mk[0, 0], Mk[0, 1])
    h, w = sizes[k]
    c4 = cv2.transform(np.float32([[0, 0], [w, 0], [w, h], [0, h]])[None], Mk[:2])[0]
    bbox[k] = (int(max(0, np.floor(c4[:, 0].min()))), int(max(0, np.floor(c4[:, 1].min()))), int(min(W, np.ceil(c4[:, 0].max()))), int(min(H, np.ceil(c4[:, 1].max()))))
    mk = cv2.imread(os.path.join(full_dir, f'{k:04d}_mask.png'), 0)
    dm = cv2.distanceTransform((mk > 0).astype(np.uint8), cv2.DIST_L2, 5)
    dm = (dm / max(float(dm.max()), 1.0)).astype(np.float32)   # 0 au bord, 1 au centre
    # garde au 1/4 (carte lisse) : 177 images pleine resolution en float32 = 2.7 Go, c'est ce qui tuait le runner
    dist_maps[k] = cv2.resize(dm, (max(1, w // DIST_DS), max(1, h // DIST_DS)), interpolation=cv2.INTER_AREA)

def distort_pts(pu, c, R):
    """undistorted -> distorted (inverse de undist), par point fixe"""
    d = pu - np.float32(c); r2u = (d[..., 0] ** 2 + d[..., 1] ** 2) / (R * R)
    f = np.ones_like(r2u)
    for _ in range(4):
        f = 1.0 / (1.0 + k1 * r2u / (f * f))      # r_d = r_u / (1 + k1 r_d^2)
    return np.float32(c) + d * f[..., None]

TILE, MARGIN, LEVELS = a.tile, 256, 5
if shrink < 1:
    # mosaique reduite (--max-side) : une tuile de 2048 px y contient des dizaines d'images ; au-dela de MAX_LAYERS images
    # par tuile, les pixels des images ecartees restaient vides (mosaique en morceaux). Tuiles a la taille d'environ 1,5 champ.
    _fp = float(np.median([max(bbox[k][2] - bbox[k][0], bbox[k][3] - bbox[k][1]) for k in comp]))
    TILE = int(min(a.tile, max(512, 64 * round(1.5 * _fp / 64))))
    MARGIN = min(256, max(64, TILE // 4))
    if TILE != a.tile: log(f'mosaïque réduite : tuiles de composition de {TILE} px (champ de {_fp:.0f} px)')
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
    def maps_of(k):
        Mi = Minv[k]
        ux = Mi[0, 0] * gx + Mi[0, 1] * gy + Mi[0, 2]; uy = Mi[1, 0] * gx + Mi[1, 1] * gy + Mi[1, 2]
        pd = distort_pts(np.stack([ux, uy], -1), centers[k], Rs[k])
        return pd[..., 0].astype(np.float32), pd[..., 1].astype(np.float32)
    def weight_of(k, mapx, mapy):
        wm = cv2.remap(dist_maps[k], mapx * (1.0 / DIST_DS), mapy * (1.0 / DIST_DS), cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        valid = wm > 0.04                                                # on reste a l'ecart du bord sombre du disque
        core = np.clip((wm - (1 - a.core)) / a.core, 0, 1) ** 3          # 1 au coeur, 0 vers le bord du champ
        return ((core + 1e-3 * valid) / (scale_k[k] ** 4)).astype(np.float32), valid
    # passe 1, incrementale : meilleur poids et image gagnante par pixel, sans rien garder d'autre (memoire bornee)
    best = np.zeros((th, tw), np.float32); lab_k = np.full((th, tw), -1, np.int32)
    for k in ks:
        mapx, mapy = maps_of(k)
        wgt, _ = weight_of(k, mapx, mapy)
        better = wgt > best
        if better.any(): best[better] = wgt[better]; lab_k[better] = k
        del mapx, mapy, wgt, better
    anyc = best > 0
    win = [k for k in ks if (lab_k == k).any()]                         # images qui gagnent quelque part
    if len(win) > MAX_LAYERS:   # garde-fou memoire : les images qui couvrent le plus de pixels de la tuile
        areas = {k: int((lab_k == k).sum()) for k in win}
        win = sorted(win, key=lambda k: -areas[k])[:MAX_LAYERS]
    lab = np.full((th, tw), -1, np.int32)
    for new_n, k in enumerate(win): lab[(lab_k == k) & anyc] = new_n
    # pixels dont le gagnant a ete ecarte : repris par la meilleure image gardee qui les couvre (2e passe ci-dessous)
    orphan = anyc & (lab < 0)
    del best, lab_k
    layers, vals, kept = [], [], win
    for n, k in enumerate(win):
        mapx, mapy = maps_of(k)
        wgt, valid = weight_of(k, mapx, mapy)
        if orphan.any():
            take = orphan & valid; lab[take] = n; orphan &= ~take
        im = cv2.remap(loader(k), mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(238, 238, 238)).astype(np.float32)
        im = np.clip(im * gains[k][None, None, :], 0, 255)
        im[~valid] = 238.0                                               # hors du disque : fond neutre (evite les halos du fondu)
        layers.append(im); vals.append(valid)
        del mapx, mapy, wgt
    anyc &= lab >= 0
    del gx, gy
    if not layers: return None
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
TILE_WORKERS = max(1, min(NWORK, 6, int(_avail_gb / 3.0)))   # ~3 Go par tuile de 2560 px en pleine charge
MAX_LAYERS = 12 if TILE == a.tile else min(40, max(12, int(12 * (a.tile + 512) ** 2 / (TILE + 2 * MARGIN) ** 2)))   # memoire ~ surface de tuile x couches
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
# --- rognage automatique : plus grand rectangle entierement couvert par les champs (bords propres, sans escalier ni trou de bordure) ---
auto_crop = None
try:
    DS = 16
    cov = np.zeros((math.ceil(H / DS), math.ceil(W / DS)), np.uint8)
    for fd in fields:
        pts = (np.array(fd['pts'], np.float32) / DS).astype(np.int32)
        cv2.fillPoly(cov, [pts], 1)
    cov = cv2.erode(cov, np.ones((5, 5), np.uint8))          # marge de deux blocs (32 px) : les bords de champ (vignettage) restent dehors
    # plus grand rectangle de 1 : histogrammes par ligne + pile (O(n*m))
    hgt = np.zeros(cov.shape[1], int); best = (0, 0, 0, 0, 0)
    for y in range(cov.shape[0]):
        hgt = np.where(cov[y] > 0, hgt + 1, 0)
        stack = []
        for x in range(cov.shape[1] + 1):
            h = hgt[x] if x < cov.shape[1] else 0
            start = x
            while stack and stack[-1][1] >= h:
                sx, sh = stack.pop(); area = sh * (x - sx)
                if area > best[0]: best = (area, sx, y - sh + 1, x, y + 1)
                start = sx
            stack.append((start, h))
    area, bx0, by0, bx1, by1 = best
    if area * DS * DS >= 0.5 * cov.sum() * DS * DS * 0.9 or area * DS * DS >= 0.35 * W * H:
        auto_crop = [int(bx0 * DS), int(by0 * DS), int(min(W, bx1 * DS)), int(min(H, by1 * DS))]
        log(f'rognage automatique proposé : {auto_crop[2] - auto_crop[0]} x {auto_crop[3] - auto_crop[1]} px ({100 * area * DS * DS / (W * H):.0f} % de la mosaïque, {100 * area / max(1, cov.sum()):.0f} % de la couverture)')
    else:
        log(f'rognage automatique : rectangle plein trop petit ({100 * area / max(1, cov.sum()):.0f} % de la couverture), lame laissée entière')
except Exception as e: log(f'rognage automatique non calculé : {e}')
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
               low_sharp_frac=round(low_frac, 3), gain_spread_pct=round(gain_spread, 1), flat_field=flat_kind or 'estimé',
               k1=round(k1, 4), zstack_fields=len(zfields), zstack_levels=ZL)
json.dump(quality, open(os.path.join(out, 'quality.json'), 'w'), indent=1)
log(f'qualité : note {grade}' + (' (' + ', '.join(notes) + ')' if notes else ' (aucune remarque)'))

# correspondance position suivie par le telephone (px du flux) -> px de la mosaique : sert a placer le premier champ d'un autre objectif
track_to_mosaic = None
try:
    tk = [k for k in comp if P[k]]
    if len(tk) >= 3:
        src_ = np.float32([P[k] for k in tk]); dst_ = np.float32([cv2.transform(np.float32([[sizes[k][1] / 2, sizes[k][0] / 2]])[None], M[k])[0][0] for k in tk])
        Atm, _ = cv2.estimateAffinePartial2D(src_, dst_, method=cv2.LMEDS)
        if Atm is not None: track_to_mosaic = Atm.tolist()
except Exception as e: log(f'correspondance suivi -> mosaïque non calculée : {e}')
json.dump(dict(size=[W, H], images=len(comp), of=N, placements=placements, k1=k1, chroma=chroma, track_to_mosaic=track_to_mosaic,
               flat_field=flat_kind or 'estimé', residual_px=res_med,
               um_per_px=um_px, zstack=ZL, quality=quality,
               title=meta.get('title'), stain=meta.get('stain'), objective=meta.get('objective'), sample=meta.get('sample'),
               comment=meta.get('comment'), parent=meta.get('parent'), hint=meta.get('hint'), stage=meta.get('stage'), captured=meta.get('captured'),
               series=meta.get('series'), component=a.component, other_components=other_components,
               parent_transform=parent_transform, parent_anchored=len(anchored_imgs) if a.parent else None, auto_crop=auto_crop, patron=meta.get('patron'),
               segment=a.segment, segments=len(segs), zone_components=zone_ids,
               params=dict(depth=DEPTH, seams=not a.no_seams, fps=a.fps, reg_size=a.reg_size, max_side=a.max_side, core=a.core, tile=a.tile,
                           distortion=not a.no_distortion, chroma=not a.no_chroma, code=(os.environ.get('GITHUB_SHA') or '')[:7] or None),
               sources=[sel[k]['path'] for k in comp]),
          open(os.path.join(out, 'layout.json'), 'w'), indent=1)
log('terminé : pano.jpg', W, 'x', H, ',', len(comp), 'images utilisées sur', N)
