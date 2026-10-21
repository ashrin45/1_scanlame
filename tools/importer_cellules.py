#!/usr/bin/env python3
"""Depose les cellules segmentees et etiquetees d'une lame (vision IA) pour le calque « Cellules » de la visionneuse.

  python tools/importer_cellules.py <slug> cellules.json [--page cellules_page.json] [--etiquettes etiquettes.json]
                                    [--taxonomie taxonomie.json] [--correspondance correspondance_matek_gfhc.json]

Verifications : largeur et hauteur egales a celles de lame.json (sinon, si la lame a ete rognee depuis, les coordonnees sont
decalees d'apres lame.json['cropped'], a condition que la taille d'origine corresponde) ; ids uniques ; polygones dans l'image ;
classes dans la taxonomie ; codes proposes dans la correspondance.
Sortie : lames/<slug>/cellules.json fusionne (contours + bord + proposition + etiquette), effectifs dans lame.json, page regeneree.
"""
import argparse, json, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from publish_lame import LAMES, load_lame, save_lame, write_pages, write_gallery

ap = argparse.ArgumentParser()
ap.add_argument('slug'); ap.add_argument('cellules')
ap.add_argument('--page', default=None); ap.add_argument('--etiquettes', default=None)
ap.add_argument('--taxonomie', default=None); ap.add_argument('--correspondance', default=None)
ap.add_argument('--force', action='store_true', help='accepter une taille differente sans decalage connu (coordonnees prises telles quelles)')
a = ap.parse_args()

j = load_lame(a.slug)
if not j: sys.exit('lame introuvable')
src = json.load(open(a.cellules, encoding='utf-8'))
page = json.load(open(a.page, encoding='utf-8')) if a.page else None
etq = json.load(open(a.etiquettes, encoding='utf-8')) if a.etiquettes else None
here = os.path.dirname(os.path.abspath(__file__))
tax = json.load(open(a.taxonomie or os.path.join(here, 'vision', 'taxonomie.json'), encoding='utf-8'))
corr = json.load(open(a.correspondance or os.path.join(here, 'vision', 'correspondance_matek_gfhc.json'), encoding='utf-8'))

# --- taille et decalage ---
W, H = int(src['largeur']), int(src['hauteur'])
dx = dy = 0
if (W, H) != (j['width'], j['height']):
    # lame rognee depuis la segmentation ? on remonte les rognages successifs
    crops = j.get('cropped') or []
    ow, oh = j['width'], j['height']
    for c in reversed(crops): ow, oh = ow + 0, oh + 0   # taille d'origine inconnue dans lame.json : on verifie par les decalages
    tot = [0, 0]
    for c in crops: tot[0] += c[0]; tot[1] += c[1]
    if crops and (W - tot[0] >= j['width'] - 2 and H - tot[1] >= j['height'] - 2) and not a.force:
        dx, dy = -tot[0], -tot[1]
        print(f'lame rognée depuis la segmentation ({W}x{H} -> {j["width"]}x{j["height"]}) : coordonnées décalées de ({dx}, {dy})')
    elif not a.force:
        sys.exit(f'taille différente : segmentation sur {W}x{H}, lame publiée {j["width"]}x{j["height"]}, et aucun rognage connu ne l\'explique (--force pour passer outre)')
W2, H2 = j['width'], j['height']

classes = {c['key']: dict(c, group=g['group']) for g in tax for c in g['classes']}
colors = {'granuleux': '#b7791f', 'mononuc': '#2b6cb0', 'immature': '#c0392b', 'erythro': '#c2185b', 'autre': '#5f6b73', 'sans': '#8a8290'}
m2g = corr.get('matek_vers_gfhc') or {}

by_page = {c['id']: c for c in (page or {}).get('cellules', [])}
by_etq = {}
for e in (etq or {}).get('etiquettes', []):
    cid = e.get('id') or int(str(e.get('doc_id', 'c0'))[1:])
    by_etq[cid] = e

seen = set(); out = []; n_out = 0; n_bad = 0
for c in src['cellules']:
    cid = int(c['id'])
    if cid in seen: sys.exit(f'id en double : {cid}')
    seen.add(cid)
    poly = [[round(x + dx), round(y + dy)] for x, y in c['polygone']]
    cx, cy = c['centroide'][0] + dx, c['centroide'][1] + dy
    inside = all(0 <= x <= W2 and 0 <= y <= H2 for x, y in poly)
    if not inside:
        n_out += 1
        if max(x for x, _ in poly) < 0 or min(x for x, _ in poly) > W2 or max(y for _, y in poly) < 0 or min(y for _, y in poly) > H2: continue   # entierement hors de la lame rognee
    diam = 2 * math.sqrt(c['aire_px'] / math.pi)
    rec = dict(id=cid, c=[round(cx), round(cy)], d=round(diam), aire=int(c['aire_px']), p=poly, sat=round(float(c.get('saturation_p92', 0)), 3), bord=False)
    pg = by_page.get(cid)
    if pg:
        rec['bord'] = bool(pg.get('bord')) or not inside
        pr = pg.get('propose')
        if pr:
            if pr['code'] not in m2g: sys.exit(f'code proposé inconnu dans la correspondance : {pr["code"]} (cellule {cid})')
            rec['propose'] = dict(code=pr['code'], cle=pr.get('cle'), niveau=pr.get('niveau'), lignee=pr.get('lignee'), conf=round(float(pr['conf']), 3), top3=pr.get('top3'))
    else: rec['bord'] = not inside
    e = by_etq.get(cid)
    if e:
        if e['classe'] not in classes: sys.exit(f'classe inconnue dans la taxonomie : {e["classe"]} (cellule {cid})')
        rec['classe'] = e['classe']; rec['quand'] = e.get('quand')
        if 'accepte' in e: rec['accepte'] = bool(e['accepte']); rec['propose_lors'] = e.get('propose'); rec['conf_lors'] = e.get('conf')
    out.append(rec)
if n_out: print(f'{n_out} contour(s) débordant de la lame publiée (rognage) : marqués bord ou retirés')

# --- effectifs et formule ---
def classe_eff(r):
    return r.get('classe') or (r['propose']['cle'] if r.get('propose') and r['propose'].get('cle') else None)
valid = [r for r in out if r.get('classe') and not r['bord']]
compt = [r for r in valid if r['classe'] != 'artefact' and classes[r['classe']]['group'] != 'autre']
formule = {}
for r in compt: formule[r['classe']] = formule.get(r['classe'], 0) + 1
stats = dict(segmentees=len(out), validees=len([r for r in out if r.get('classe')]), propositions=len([r for r in out if r.get('propose') and not r.get('classe')]),
             bord=len([r for r in out if r['bord']]), comptees=len(compt), formule=formule,
             acceptees=len([r for r in out if r.get('accepte') is True]), corrigees=len([r for r in out if r.get('accepte') is False]))
merged = dict(slug=a.slug, largeur=W2, hauteur=H2, source=src.get('source'), segmentation=dict(objets_total=src.get('objets_total'), critere=src.get('critere'), facteur_travail=src.get('facteur_travail'), douteuses=src.get('douteuses'), decalage=[dx, dy]),
              modele=(page or {}).get('propositions'), taxonomie=[dict(group=g['group'], label=g['group_label'], color=colors.get(g['group'], '#8a8290'), classes=[dict(key=c['key'], label=c['label'], abbrev=c['abbrev']) for c in g['classes']]) for g in tax],
              correspondance=m2g, stats=stats, cellules=out)
d = os.path.join(LAMES, a.slug)
json.dump(merged, open(os.path.join(d, 'cellules.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
j['cellules'] = dict(n=stats['segmentees'], validees=stats['validees'], propositions=stats['propositions'], bord=stats['bord'], comptees=stats['comptees'])
save_lame(a.slug, j); write_pages(a.slug); write_gallery()
print(f'{a.slug} : {stats["segmentees"]} cellules ({stats["validees"]} validées, {stats["propositions"]} avec seule une proposition, {stats["bord"]} au bord, {stats["comptees"]} comptées dans la formule) -> lames/{a.slug}/cellules.json ({os.path.getsize(os.path.join(d, "cellules.json")) >> 10} ko)')
