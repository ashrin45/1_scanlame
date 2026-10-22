#!/usr/bin/env python3
"""Publie une lame assemblee sous forme de pyramide de tuiles + visionneuse dans lames/<nom>/,
puis regenere la galerie lames/index.html et la liste lames/lames.json. Un `git push` suffit ensuite (GitHub Pages).

Usage : python tools/publish_lame.py pano.jpg --name gram-2026-10-14 --title "Gram, prelevement vaginal"
        [--fields fields.json] [--layout layout.json] [--um-per-px 0.12] [--tile 512] [--quality 85]

Avec --layout (ecrit par pipeline.py) : metadonnees (coloration, objectif, prelevement...), rapport de qualite,
echelle et z-stack (pano_z*.jpg a cote du pano) sont repris automatiquement.
Les fonctions write_pages(slug) et write_gallery() servent aussi a link_child.py.
"""
import argparse, json, math, os, re, html, glob, datetime
import cv2, numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMES = os.path.join(ROOT, 'lames')
STAINS = {'gram': 'Gram', 'mgg': 'MGG', 'giemsa': 'Giemsa', 'ziehl': 'Ziehl-Neelsen', 'auramine': 'Auramine', 'bleu': 'Bleu de méthylène',
          'frais': 'État frais', 'encre': 'Encre de Chine', 'lactophenol': 'Lactophénol', 'he': 'HE', 'autre': 'Autre'}

def slugify(t):
    return re.sub(r'[^a-z0-9-]+', '-', t.lower()).strip('-')

def build_pyramid(img, tiles, tile, quality):
    """Pyramide Deep Zoom complete. Retourne (W, H, maxlvl, nb tuiles)."""
    H, W = img.shape[:2]
    maxlvl = math.ceil(math.log2(max(W, H)))
    n = 0
    for lvl in range(maxlvl, -1, -1):
        lh, lw = img.shape[:2]
        d = os.path.join(tiles, str(lvl)); os.makedirs(d, exist_ok=True)
        for ty in range(math.ceil(lh / tile)):
            for tx in range(math.ceil(lw / tile)):
                x0, y0 = max(0, tx * tile - 1), max(0, ty * tile - 1)
                x1, y1 = min(lw, (tx + 1) * tile + 1), min(lh, (ty + 1) * tile + 1)
                cv2.imwrite(os.path.join(d, f'{tx}_{ty}.jpg'), img[y0:y1, x0:x1], [cv2.IMWRITE_JPEG_QUALITY, quality]); n += 1
        if lvl == 0: break
        img = cv2.resize(img, (max(1, math.ceil(lw / 2)), max(1, math.ceil(lh / 2))), interpolation=cv2.INTER_AREA)
    return W, H, maxlvl, n

def build_z_pyramid(z, img, base_tiles, out_dir, tile, quality, keys, thresh=1.5):
    """Tuiles du plan z qui different de la tuile de base ; les autres ne sont pas ecrites (la visionneuse retombe sur la base)."""
    H, W = img.shape[:2]
    maxlvl = math.ceil(math.log2(max(W, H)))
    n = 0
    for lvl in range(maxlvl, -1, -1):
        lh, lw = img.shape[:2]
        for ty in range(math.ceil(lh / tile)):
            for tx in range(math.ceil(lw / tile)):
                x0, y0 = max(0, tx * tile - 1), max(0, ty * tile - 1)
                x1, y1 = min(lw, (tx + 1) * tile + 1), min(lh, (ty + 1) * tile + 1)
                t = img[y0:y1, x0:x1]
                b = cv2.imread(os.path.join(base_tiles, str(lvl), f'{tx}_{ty}.jpg'))
                if b is None or b.shape != t.shape or np.abs(b.astype(np.int16) - t.astype(np.int16)).mean() > thresh:
                    d = os.path.join(out_dir, str(lvl)); os.makedirs(d, exist_ok=True)
                    cv2.imwrite(os.path.join(d, f'{tx}_{ty}.jpg'), t, [cv2.IMWRITE_JPEG_QUALITY, quality]); n += 1
                    keys.add(f'{lvl}/{tx}_{ty}')
        if lvl == 0: break
        img = cv2.resize(img, (max(1, math.ceil(lw / 2)), max(1, math.ceil(lh / 2))), interpolation=cv2.INTER_AREA)
    return n

def load_lame(slug):
    p = os.path.join(LAMES, slug, 'lame.json')
    if not os.path.isfile(p): return None
    j = json.load(open(p, encoding='utf-8'))
    # lames publiees avant ces champs
    j.setdefault('maxlvl', math.ceil(math.log2(max(j['width'], j['height'])))); j.setdefault('tile', 512)
    return j

def save_lame(slug, j):
    json.dump(j, open(os.path.join(LAMES, slug, 'lame.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

def params_summary(pr):
    bits = ['profondeur ' + ('on' if pr.get('depth') == 'on' else 'off'), 'coutures ' + ('fond' if pr.get('seams', True) else 'droites')]
    if pr.get('reg_size'): bits.append(f'recalage {pr["reg_size"]} px')
    if pr.get('code'): bits.append('code ' + pr['code'])
    return ', '.join(bits)

def info_line(j):
    parts = []
    if j.get('stain'): parts.append(STAINS.get(j['stain'], j['stain']))
    if j.get('objective'): parts.append('objectif ' + str(j['objective']))
    if j.get('sample'): parts.append(j['sample'])
    if j.get('captured'): parts.append(j['captured'])
    return ' · '.join(html.escape(p) for p in parts)

def write_pages(slug):
    """(Re)genere lames/<slug>/index.html a partir de lame.json + fields.json."""
    out = os.path.join(LAMES, slug)
    j = load_lame(slug)
    W, H = j['width'], j['height']
    fields = json.load(open(os.path.join(out, 'fields.json'))) if os.path.isfile(os.path.join(out, 'fields.json')) else []
    q = j.get('quality') or {}
    meta = ''
    if fields: meta += f'<span><b>{len(fields)}</b> champs</span>'
    meta += f'<span><b>{W} × {H}</b> px</span><span><b>{j["maxlvl"] + 1}</b> niveaux de zoom</span>'
    meta += f'<span>{j["um_per_px"]:.4g} µm/px</span>' if j.get('um_per_px') else '<span>échelle non calibrée</span>'
    if j.get('zstack'): meta += f'<span><b>{j["zstack"]}</b> plans de profondeur</span>'
    if j.get('retakes'): meta += f'<span>complétée <b>{j["retakes"]}</b> fois</span>'
    pr = j.get('params') or {}
    if pr: meta += '<span>v' + str(j.get('version') or 1) + ' · ' + html.escape(params_summary(pr)) + '</span>'
    logs = sorted(os.listdir(os.path.join(out, 'journaux'))) if os.path.isdir(os.path.join(out, 'journaux')) else []
    if logs: meta += '<span>journaux : ' + ' '.join(f'<a href="journaux/{html.escape(l)}">{"téléphone" if "telephone" in l else "planche" if "planche" in l else "assemblage"} {html.escape(l[:8])}</a>' for l in logs) + '</span>'
    children = []
    for c in j.get('children') or []:
        cj = load_lame(c['slug'])
        if cj: children.append(dict(slug=c['slug'], title=cj['title'], objective=cj.get('objective'), width=cj['width'], height=cj['height'], maxlvl=cj['maxlvl'], tile=cj['tile'], transform=c.get('transform'), outline=cj.get('outline'), cellules=cj.get('cellules') if os.path.isfile(os.path.join(LAMES, c['slug'], 'cellules.json')) else None))
    parent = None
    if j.get('parent'):
        pj = load_lame(j['parent'])
        if pj: parent = dict(slug=j['parent'], title=pj['title'], placed=bool(j.get('parent_transform')))
    tpl = open(os.path.join(ROOT, 'tools', 'viewer_template.html'), encoding='utf-8').read()
    page = (tpl.replace('__TITLE__', html.escape(j['title'])).replace('__INFO__', info_line(j)).replace('__META__', meta)
            .replace('__SLUG_TXT__', html.escape(slug)).replace('__SLUG__', json.dumps(slug)).replace('__W__', str(W)).replace('__H__', str(H)).replace('__TILE__', str(j['tile'])).replace('__MAXLVL__', str(j['maxlvl']))
            .replace('__FIELDS__', json.dumps(fields, separators=(',', ':'))).replace('__UMPX__', json.dumps(j.get('um_per_px')))
            .replace('__ZSTACK__', json.dumps(dict(levels=j['zstack'], tiles=j['zstack_tiles']) if j.get('zstack') else None))
            .replace('__QUALITY__', json.dumps(q, ensure_ascii=False)).replace('__CELLS__', json.dumps(j.get('cellules') if os.path.isfile(os.path.join(out, 'cellules.json')) else None)).replace('__CHILDREN__', json.dumps(children, ensure_ascii=False))
            .replace('__PARENT__', json.dumps(parent, ensure_ascii=False)).replace('__FAVORI__', json.dumps(bool(j.get('favori')))))
    open(os.path.join(out, 'index.html'), 'w', encoding='utf-8').write(page)

def write_offline_zip(slug, out_zip):
    """Dossier autonome de la lame : visionneuse HTML (OpenSeadragon inclus) + tuiles + lame.json/fields.json, dans un zip.
    S'ouvre hors ligne en double-cliquant sur index.html (les calques de detail et le bouton Modifier sont retires)."""
    import zipfile
    d = os.path.join(LAMES, slug)
    page = open(os.path.join(d, 'index.html'), encoding='utf-8').read()
    page = page.replace('https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/openseadragon.min.js', 'openseadragon.min.js')
    page = re.sub(r'<script src="https://cdnjs\.cloudflare\.com/ajax/libs/qrcodejs[^>]*></script>\n', '', page)
    page = re.sub(r'<link rel="stylesheet" href="https://fonts\.googleapis\.com[^>]*>', '', page)
    page = re.sub(r'const CHILDREN = .*?;\n', 'const CHILDREN = [];\n', page, count=1)
    page = re.sub(r'const PARENT = .*?;\n', 'const PARENT = null;\n', page, count=1)
    page = page.replace('<button id="editbtn" aria-pressed="false"', '<button id="editbtn" hidden aria-pressed="false"')
    page = page.replace('<button id="favbtn" aria-pressed="false"', '<button id="favbtn" hidden aria-pressed="false"')
    ap = os.path.join(d, 'annotations.json')     # annotations incluses dans la page (lecture seule, sans serveur)
    page = page.replace('const ANNOT_EMBED = null;', 'const ANNOT_EMBED = ' + (open(ap, encoding='utf-8').read().strip() if os.path.isfile(ap) else '{"annotations":[]}') + ';', 1)
    page = page.replace('<div class="meta">', '<div class="meta"><span style="opacity:.7">copie hors ligne</span> · ', 1)
    osd = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor', 'openseadragon.min.js')
    with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(f'{slug}/index.html', page)
        z.write(osd, f'{slug}/openseadragon.min.js')
        z.writestr(f'{slug}/LISEZMOI.txt', f'Lame « {slug} » de ScanLame.\nOuvrir index.html dans un navigateur (double-clic) : zoom, déplacement, champs, qualité, hors ligne.\n'
                   'lame.json : métadonnées (taille, objectif, µm/px) ; fields.json : position de chaque photo ; lame_files/ : tuiles Deep Zoom (le dernier niveau = pleine résolution).\n')
        for f in ('lame.json', 'fields.json', 'lame.dzi', 'cellules.json'):
            if os.path.isfile(os.path.join(d, f)): z.write(os.path.join(d, f), f'{slug}/{f}')
        tiles = os.path.join(d, 'lame_files')
        for root, _, files in os.walk(tiles):
            for f in files: z.write(os.path.join(root, f), f'{slug}/lame_files/{os.path.relpath(os.path.join(root, f), tiles)}')
    return out_zip

def write_gallery():
    items = []
    for d in sorted(os.listdir(LAMES)):
        j = load_lame(d)
        if j:
            thumb = f'{d}/lame_files/{max(0, j["maxlvl"] - 4)}/0_0.jpg'
            items.append(dict(slug=d, title=j['title'], width=j['width'], height=j['height'], stain=j.get('stain'), objective=j.get('objective'),
                              sample=j.get('sample'), captured=j.get('captured'), um_per_px=j.get('um_per_px'), grade=(j.get('quality') or {}).get('grade'),
                              zstack=j.get('zstack') or 0, parent=j.get('parent'), children=[c['slug'] for c in (j.get('children') or [])], thumb=thumb, tile=j.get('tile'), maxlvl=j.get('maxlvl'), cellules=j.get('cellules'),
                              published=j.get('published'), updated=j.get('updated'), series=j.get('series'), version=j.get('version') or 1, params=j.get('params'),
                              favori=bool(j.get('favori')), annotations=j.get('annotations') or 0))
    json.dump(items, open(os.path.join(LAMES, 'lames.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    tpl = open(os.path.join(ROOT, 'tools', 'gallery_template.html'), encoding='utf-8').read()
    open(os.path.join(LAMES, 'index.html'), 'w', encoding='utf-8').write(
        tpl.replace('__ITEMS__', json.dumps(items, ensure_ascii=False)).replace('__STAINS__', json.dumps(STAINS, ensure_ascii=False)))
    return len(items)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pano')
    ap.add_argument('--name', required=True, help='slug du dossier (lettres, chiffres, tirets)')
    ap.add_argument('--title', default=None)
    ap.add_argument('--fields', default=None, help='fields.json : contours des champs [{id, pts:[[x,y],...]}]')
    ap.add_argument('--layout', default=None, help='layout.json du pipeline (metadonnees, qualite, echelle, z-stack)')
    ap.add_argument('--tile', type=int, default=512)
    ap.add_argument('--quality', type=int, default=90)
    ap.add_argument('--um-per-px', type=float, default=None, help='micromètres par pixel de la mosaïque (barre d\'échelle)')
    a = ap.parse_args()

    slug = slugify(a.name)
    out = os.path.join(LAMES, slug)
    tiles = os.path.join(out, 'lame_files')
    os.makedirs(tiles, exist_ok=True)
    layout = json.load(open(a.layout, encoding='utf-8')) if a.layout and os.path.isfile(a.layout) else {}
    img = cv2.imread(a.pano)
    W, H, maxlvl, n = build_pyramid(img, tiles, a.tile, a.quality)
    open(os.path.join(out, 'lame.dzi'), 'w').write(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" Format="jpg" Overlap="1" TileSize="{a.tile}"><Size Width="{W}" Height="{H}"/></Image>')
    # version reduite pour recaler d'eventuelles lames enfants (autre objectif) dans celle-ci
    s = min(1.0, 3000 / max(W, H))
    cv2.imwrite(os.path.join(out, 'pano_reg.jpg'), cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    del img
    # z-stack : pano_z*.jpg a cote du pano
    zpaths = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(a.pano)), 'pano_z*.jpg')), key=lambda p: int(re.search(r'_z(\d+)', p).group(1)))
    zkeys, zn = set(), 0
    for z, zp in enumerate(zpaths):
        zn += build_z_pyramid(z, cv2.imread(zp), tiles, os.path.join(out, f'lame_z{z}'), a.tile, a.quality, zkeys)
    if zpaths: print(f'z-stack : {len(zpaths)} plans, {zn} tuiles spécifiques')

    if a.fields and os.path.isfile(a.fields):
        fields = json.load(open(a.fields))
        json.dump(fields, open(os.path.join(out, 'fields.json'), 'w'), separators=(',', ':'))
    else:
        fields = []
    title = a.title or layout.get('title') or slug
    captured = None
    m = re.search(r'(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})', str(layout.get('series') or ''))
    if m: captured = f'{m[3]}/{m[2]}/{m[1]} {m[4]}:{m[5]}'
    old = load_lame(slug) or {}
    j = dict(title=title, width=W, height=H, fields=len(fields), tiles=n, tile=a.tile, maxlvl=maxlvl,
             um_per_px=a.um_per_px or layout.get('um_per_px'),
             stain=layout.get('stain'), objective=layout.get('objective'), sample=layout.get('sample'), comment=layout.get('comment'),
             captured=captured or old.get('captured') or datetime.date.today().strftime('%d/%m/%Y'),
             parent=layout.get('parent') or old.get('parent'), parent_transform=layout.get('parent_transform') or old.get('parent_transform'), parent_anchored=layout.get('parent_anchored') if layout.get('parent_transform') else old.get('parent_anchored'), parent_approx=old.get('parent_approx'),
             outline=old.get('outline'), cellules=old.get('cellules'), hint=layout.get('hint') or old.get('hint'), stage=layout.get('stage') or old.get('stage'), stage_cal=old.get('stage_cal'), stage_obs=old.get('stage_obs'),
             children=old.get('children') or [], quality=layout.get('quality'),
             series=layout.get('series') or old.get('series'), version=layout.get('version') or old.get('version') or 1, params=layout.get('params') or old.get('params'),
             published=old.get('published') or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
             updated=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
             retakes=layout.get('retakes') or old.get('retakes') or 0, retake_series=layout.get('retake_series') or old.get('retake_series') or [],
             zstack=len(zpaths) if zpaths else (old.get('zstack') or 0), zstack_tiles=sorted(zkeys) if zpaths else old.get('zstack_tiles'))
    save_lame(slug, j)
    write_pages(slug)
    # la lame parente (si deja publiee) affiche ses enfants : on la regenere aussi
    if j['parent'] and load_lame(j['parent']): write_pages(j['parent'])
    for c in j['children']:
        if load_lame(c['slug']): write_pages(c['slug'])
    k = write_gallery()
    print(f'{slug}: {W}x{H}, {n} tuiles, {k} lame(s) dans la galerie')

if __name__ == '__main__':
    main()
