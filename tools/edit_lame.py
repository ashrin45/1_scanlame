#!/usr/bin/env python3
"""Modifications d'une lame publiee, demandees depuis la visionneuse (boutons « Rogner » et « Supprimer »).

Usage :
  python tools/edit_lame.py delete <slug>
  python tools/edit_lame.py crop <slug> <x0> <y0> <x1> <y1>      (px pleine resolution de la lame)

La demande arrive par la fonction Supabase (route /finish avec action) : process_inbox la lit et appelle ce script.
Le rognage reconstruit la zone gardee a partir des tuiles, refait la pyramide, decale les champs, le contour,
la transformation vers la lame d'ensemble et celles des lames de detail. Les plans de profondeur (z-stack) sont abandonnes.
"""
import json, math, os, shutil, sys
import cv2, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from publish_lame import LAMES, load_lame, save_lame, write_pages, write_gallery, build_pyramid, build_z_pyramid

def shift_transform(t, dx, dy):
    """t : [a, b, tx, ty] applique a des px de la lame rognee (x' = x - dx) : x_parent = a x - b y + tx -> tx' = tx + a dx - b dy"""
    a, b, tx, ty = t
    return [a, b, tx + a * dx - b * dy, ty + b * dx + a * dy]

def delete(slug):
    j = load_lame(slug)
    if not j: sys.exit('lame introuvable')
    touched = []
    if j.get('parent'):
        pj = load_lame(j['parent'])
        if pj:
            pj['children'] = [c for c in (pj.get('children') or []) if c['slug'] != slug]
            pj['stage_obs'] = [o for o in (pj.get('stage_obs') or []) if o.get('slug') != slug] or None
            save_lame(j['parent'], pj); touched.append(j['parent'])
    for c in j.get('children') or []:
        cj = load_lame(c['slug'])
        if cj:
            cj['parent'] = None; cj['parent_transform'] = None; cj['parent_anchored'] = None
            save_lame(c['slug'], cj); touched.append(c['slug'])
    shutil.rmtree(os.path.join(LAMES, slug))
    for t in touched: write_pages(t)
    write_gallery()
    print(f'lame {slug} supprimée' + (f' ; lames mises à jour : {touched}' if touched else ''))
    return touched

def crop(slug, x0, y0, x1, y1):
    j = load_lame(slug)
    if not j: sys.exit('lame introuvable')
    W, H, T, maxlvl = j['width'], j['height'], j['tile'], j['maxlvl']
    x0, y0 = max(0, int(x0)), max(0, int(y0)); x1, y1 = min(W, int(x1)), min(H, int(y1))
    if x1 - x0 < 64 or y1 - y0 < 64: sys.exit('zone trop petite')
    if (x0, y0, x1, y1) == (0, 0, W, H): print('rien à rogner'); return
    d = os.path.join(LAMES, slug); tiles = os.path.join(d, 'lame_files')
    # zone gardee, reconstruite depuis les tuiles du niveau plein
    img = np.full((y1 - y0, x1 - x0, 3), 255, np.uint8)
    for ty in range(y0 // T, math.ceil(y1 / T)):
        for tx in range(x0 // T, math.ceil(x1 / T)):
            t = cv2.imread(os.path.join(tiles, str(maxlvl), f'{tx}_{ty}.jpg'))
            if t is None: continue
            ox, oy = max(0, tx * T - 1), max(0, ty * T - 1)          # tuiles Deep Zoom : 1 px de recouvrement
            sx0, sy0 = max(x0, ox), max(y0, oy); sx1, sy1 = min(x1, ox + t.shape[1]), min(y1, oy + t.shape[0])
            if sx1 <= sx0 or sy1 <= sy0: continue
            img[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = t[sy0 - oy:sy1 - oy, sx0 - ox:sx1 - ox]
    # plans de profondeur : meme zone, tuiles propres au plan collees sur la base (les autres retombent sur la base)
    zimgs = []
    for z in range(int(j.get('zstack') or 0)):
        zd = os.path.join(d, f'lame_z{z}', str(maxlvl)); zi = img.copy()
        if os.path.isdir(zd):
            for fn in os.listdir(zd):
                tx, ty = (int(v) for v in fn[:-4].split('_'))
                t = cv2.imread(os.path.join(zd, fn))
                if t is None: continue
                ox, oy = max(0, tx * T - 1), max(0, ty * T - 1)
                sx0, sy0 = max(x0, ox), max(y0, oy); sx1, sy1 = min(x1, ox + t.shape[1]), min(y1, oy + t.shape[0])
                if sx1 <= sx0 or sy1 <= sy0: continue
                zi[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = t[sy0 - oy:sy1 - oy, sx0 - ox:sx1 - ox]
        zimgs.append(zi)
    shutil.rmtree(tiles); os.makedirs(tiles)
    for f in os.listdir(d):
        if f.startswith('lame_z') and os.path.isdir(os.path.join(d, f)): shutil.rmtree(os.path.join(d, f))
    nW, nH, nmax, n = build_pyramid(img, tiles, T, 90)
    zkeys = set()
    for z, zi in enumerate(zimgs): build_z_pyramid(z, zi, tiles, os.path.join(d, f'lame_z{z}'), T, 90, zkeys)
    del zimgs
    open(os.path.join(d, 'lame.dzi'), 'w').write(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" Format="jpg" Overlap="1" TileSize="{T}"><Size Width="{nW}" Height="{nH}"/></Image>')
    s = min(1.0, 3000 / max(nW, nH))
    cv2.imwrite(os.path.join(d, 'pano_reg.jpg'), cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    # champs : decales, ceux entierement hors zone retires
    fp = os.path.join(d, 'fields.json')
    if os.path.isfile(fp):
        fields = json.load(open(fp)); kept = []
        for f in fields:
            pts = [[x - x0, y - y0] for x, y in f['pts']]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            if max(xs) < 0 or max(ys) < 0 or min(xs) > nW or min(ys) > nH: continue
            f['pts'] = pts; kept.append(f)
        json.dump(kept, open(fp, 'w'))
        j['fields'] = len(kept)
    if j.get('outline'):
        def clip_rect(poly, w, h):
            # Sutherland-Hodgman contre le rectangle [0,w]x[0,h]
            out = [list(p) for p in poly]
            for axis, lim, keep_le in ((0, 0, False), (0, w, True), (1, 0, False), (1, h, True)):
                inp, out = out, []
                for k in range(len(inp)):
                    p, q = inp[k], inp[(k + 1) % len(inp)]
                    pin = (p[axis] <= lim) if keep_le else (p[axis] >= lim); qin = (q[axis] <= lim) if keep_le else (q[axis] >= lim)
                    def inter():
                        t = (lim - p[axis]) / ((q[axis] - p[axis]) or 1e-9); return [p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])]
                    if pin:
                        out.append(p)
                        if not qin: out.append(inter())
                    elif qin: out.append(inter())
                if not out: break
            return [[round(x, 1), round(y, 1)] for x, y in out]
        j['outline'] = [cp for cp in (clip_rect([[x - x0, y - y0] for x, y in poly], nW, nH) for poly in j['outline']) if len(cp) >= 3]
    if j.get('parent_transform'):
        j['parent_transform'] = shift_transform(j['parent_transform'], x0, y0)
        pj = load_lame(j['parent'])
        if pj:
            for c in pj.get('children') or []:
                if c['slug'] == slug: c['transform'] = j['parent_transform']
            save_lame(j['parent'], pj); write_pages(j['parent'])
    child_crops = []
    for c in j.get('children') or []:
        if c.get('transform'):
            a, b, tx, ty = c['transform']; c['transform'] = [a, b, tx - x0, ty - y0]
            cj = load_lame(c['slug'])
            if cj:
                cj['parent_transform'] = c['transform']; save_lame(c['slug'], cj); write_pages(c['slug'])
                # la partie du detail qui sort de la zone gardee est rognee aussi : rectangle garde ramene dans les px du detail
                A = np.array([[a, -b], [b, a]]); Ai = np.linalg.inv(A)
                corners = np.array([[0, 0], [nW, 0], [nW, nH], [0, nH]], float) - [tx - x0, ty - y0]
                cc = (Ai @ corners.T).T
                cx0, cy0 = np.floor(cc.min(0)); cx1, cy1 = np.ceil(cc.max(0))
                cx0, cy0 = max(0, int(cx0)), max(0, int(cy0)); cx1, cy1 = min(cj['width'], int(cx1)), min(cj['height'], int(cy1))
                if (cx0, cy0, cx1, cy1) != (0, 0, cj['width'], cj['height']) and cx1 - cx0 >= 64 and cy1 - cy0 >= 64:
                    child_crops.append((c['slug'], cx0, cy0, cx1, cy1))
    if j.get('quality'):
        j['quality']['holes'] = []; j['quality']['holes_frac'] = 0.0
    j.update(width=nW, height=nH, maxlvl=nmax, tiles=n, zstack=(j.get('zstack') or 0) if zkeys else 0, zstack_tiles=sorted(zkeys) if zkeys else None)
    j['cropped'] = (j.get('cropped') or []) + [[x0, y0, x1, y1]]
    import datetime
    j['updated'] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    save_lame(slug, j); write_pages(slug); write_gallery()
    print(f'lame {slug} rognée : {W}x{H} -> {nW}x{nH} ({n} tuiles)')
    for (cs, cx0, cy0, cx1, cy1) in child_crops:
        print(f'  détail {cs} : rogné à la zone gardée')
        crop(cs, cx0, cy0, cx1, cy1)

def favori(slug, on):
    """Ajoute la lame aux favoris de la galerie (ou l'en retire)."""
    j = load_lame(slug)
    if not j: sys.exit('lame introuvable')
    if on: j['favori'] = True
    else: j.pop('favori', None)
    save_lame(slug, j); write_pages(slug); write_gallery()
    print(f'lame {slug} : {"ajoutée aux" if on else "retirée des"} favoris')

COULEURS = ('#e8a33d', '#e5484d', '#30a46c', '#3e63dd', '#8e4ec6')

def annotation(slug, cmd):
    """Applique une demande d'annotation venue de la visionneuse : {op: add | update | delete, a: {...}, id, maj}.
    Fichier lames/<slug>/annotations.json ; le nombre est repris dans lame.json (galerie)."""
    import re, datetime
    j = load_lame(slug)
    if not j: sys.exit('lame introuvable')
    W, H = j['width'], j['height']
    path = os.path.join(LAMES, slug, 'annotations.json')
    data = json.load(open(path, encoding='utf-8')) if os.path.isfile(path) else {'slug': slug, 'annotations': []}
    lst = data['annotations']
    op = cmd.get('op'); src = cmd.get('a') or {}
    aid = str(src.get('id') or cmd.get('id') or '')
    if not re.fullmatch(r'[a-z0-9]{6,32}', aid): print(f'annotation : identifiant invalide « {aid} »'); return
    maj = str(cmd.get('maj') or src.get('maj') or '')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T[\d:.]+Z?', maj): maj = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    cur = next((x for x in lst if x['id'] == aid), None)
    if op == 'delete':
        if cur: lst.remove(cur); print(f'annotation {aid} supprimée de {slug}')
        else: print(f'annotation {aid} déjà absente de {slug}')
    elif op in ('add', 'update'):
        def num(v, lo, hi):
            v = float(v)
            if not math.isfinite(v): raise ValueError('coordonnée invalide')
            return round(min(hi, max(lo, v)), 1)
        rec = dict(cur or {}, id=aid)
        typ = src.get('type', rec.get('type'))
        if typ not in ('point', 'zone'): print(f'annotation {aid} : type invalide {typ}'); return
        rec['type'] = typ
        try:
            if typ == 'point' and ('x' in src or 'x' not in rec):
                rec['x'], rec['y'] = num(src['x'], 0, W), num(src['y'], 0, H); rec.pop('rect', None)
            if typ == 'zone' and ('rect' in src or 'rect' not in rec):
                x0, y0, x1, y1 = (float(v) for v in src['rect'])
                x0, x1 = sorted((num(x0, 0, W), num(x1, 0, W))); y0, y1 = sorted((num(y0, 0, H), num(y1, 0, H)))
                if x1 - x0 < 2 or y1 - y0 < 2: print(f'annotation {aid} : zone vide'); return
                rec['rect'] = [x0, y0, x1, y1]; rec.pop('x', None); rec.pop('y', None)
        except (KeyError, TypeError, ValueError) as e:
            print(f'annotation {aid} : coordonnées invalides ({e})'); return
        if 'texte' in src: rec['texte'] = str(src['texte'])[:2000].strip()
        if 'auteur' in src: rec['auteur'] = str(src['auteur'])[:80].strip()
        if 'couleur' in src: rec['couleur'] = src['couleur'] if src['couleur'] in COULEURS else COULEURS[0]
        rec.setdefault('couleur', COULEURS[0]); rec.setdefault('texte', ''); rec.setdefault('auteur', '')
        rec.setdefault('cree', maj); rec['maj'] = maj
        if cur: lst[lst.index(cur)] = rec
        else: lst.append(rec)
        print(f'annotation {aid} {"modifiée" if cur else "ajoutée"} sur {slug}')
    else:
        print(f'annotation : opération inconnue {op}'); return
    lst.sort(key=lambda x: x.get('cree') or '')
    if lst: json.dump(data, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    elif os.path.isfile(path): os.remove(path)
    if lst: j['annotations'] = len(lst)
    else: j.pop('annotations', None)
    save_lame(slug, j); write_gallery()

if __name__ == '__main__':
    if sys.argv[1] == 'delete': delete(sys.argv[2])
    elif sys.argv[1] == 'crop': crop(sys.argv[2], *sys.argv[3:7])
    elif sys.argv[1] == 'favori': favori(sys.argv[2], sys.argv[3].lower() in ('on', 'oui', '1', 'true'))
    elif sys.argv[1] == 'annotation': annotation(sys.argv[2], json.loads(sys.argv[3]))
    else: sys.exit('action inconnue')
