#!/usr/bin/env python3
"""Tests de non-regression du pipeline sur des prises brutes de reference.

  python tools/test_pipeline.py <dossier des prises> [--only pv-multi] [--record] [--quick]

<dossier des prises> contient un sous-dossier par serie (serie.json + photos/), tel que l'artefact brut-<serie> de GitHub Actions.
Pour chaque serie listee dans tools/tests/reference.json, le pipeline est lance comme process_inbox le ferait (segment
d'ensemble = objectif le plus faible, autres segments ancres dedans), les chiffres sont releves et compares aux references
avec des tolerances. --record remplace les references par les valeurs mesurees (a faire sciemment, apres verification visuelle).
--quick ne joue que les series marquees quick (pour GitHub Actions). Code de sortie 1 si un ecart depasse la tolerance.

Chiffres releves par segment : images utilisees, erreur mediane et max de recalage (px recalage), taille de la mosaique,
rognage automatique, et pour un detail : images ancrees, residu d'ancrage, echelle et position (tx, ty) dans l'ensemble.
"""
import argparse, json, math, os, re, subprocess, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, 'tools', 'tests', 'reference.json')

ap = argparse.ArgumentParser()
ap.add_argument('raw')
ap.add_argument('--only', default=None)
ap.add_argument('--record', action='store_true')
ap.add_argument('--quick', action='store_true')
ap.add_argument('--max-side', type=int, default=None, help='reduire la composition (plus rapide ; les positions sont comparees a l echelle)')
a = ap.parse_args()

ref = json.load(open(REF)) if os.path.isfile(REF) else {'series': {}}

def objective_number(o):
    m = re.search(r'(\d+(?:[.,]\d+)?)', str(o or '')); return float(m[1].replace(',', '.')) if m else 1e9

def parse_log(path):
    """Chiffres tires de pipeline.log."""
    out = {}
    for line in open(path, encoding='utf-8', errors='replace'):
        m = re.search(r'recalage : erreur médiane ([\d.]+) px .*max ([\d.]+)', line)
        if m: out['residual_med'] = float(m[1]); out['residual_max'] = float(m[2])
        m = re.search(r'terminé : pano.jpg (\d+) x (\d+) , (\d+) images utilisées sur (\d+)', line)
        if m: out['width'], out['height'], out['used'], out['candidates'] = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        m = re.search(r'ancrage : (\d+)/(\d+) groupes appariés, (\d+) images ancrées', line)
        if m: out['groups_ok'], out['groups'], out['anchored_pass1'] = int(m[1]), int(m[2]), int(m[3])
        m = re.search(r'ancrages \(final\) : résidu médian ([\d.]+)', line)
        if m: out['anchor_residual'] = float(m[1])
        m = re.search(r'position dans la lame d\'ensemble : échelle ([\d.]+), rotation ([-\d.]+)°, (\d+) images ancrées', line)
        if m: out['scale'], out['rotation'], out['anchored'] = float(m[1]), float(m[2]), int(m[3])
        m = re.search(r'rognage automatique proposé : (\d+) x (\d+) px', line)
        if m: out['crop_w'], out['crop_h'] = int(m[1]), int(m[2])
        m = re.search(r'qualité : note ([ABC])', line)
        if m: out['grade'] = m[1]
    return out

def run(series_dir, out, extra):
    os.makedirs(out, exist_ok=True)
    cmd = [sys.executable, os.path.join(ROOT, 'tools', 'pipeline.py'), series_dir, '--out', out] + extra + (['--max-side', str(a.max_side)] if a.max_side else [])
    t0 = time.time(); rc = subprocess.run(cmd, capture_output=True, text=True).returncode
    return rc, time.time() - t0

def check(name, got, want, tol):
    """Compare un releve a sa reference ; retourne la liste des ecarts."""
    errs = []
    for k, v in want.items():
        if k not in got: errs.append(f'{k} : absent'); continue
        g = got[k]
        if isinstance(v, str):
            if g != v: errs.append(f'{k} : {g} (référence {v})')
        else:
            t = tol.get(k, 0.05)
            if abs(g - v) > max(t * abs(v), tol.get(k + '_abs', 0)): errs.append(f'{k} : {g} (référence {v}, tolérance {t * 100:.0f} %)')
    return errs

TOL = {'residual_med': 0.15, 'residual_max': 0.5, 'width': 0.02, 'height': 0.02, 'used': 0.0, 'candidates': 0.0, 'anchored': 0.25, 'anchor_residual': 0.3,
       'scale': 0.02, 'rotation': 0.0, 'rotation_abs': 1.0, 'crop_w': 0.05, 'crop_h': 0.05, 'groups_ok': 0.0, 'groups_ok_abs': 2, 'tx': 0.0, 'tx_abs': 25, 'ty': 0.0, 'ty_abs': 25}

results = {}; failed = False
for name, spec in ref['series'].items():
    if a.only and name != a.only: continue
    if a.quick and not spec.get('quick'): continue
    sdir = os.path.join(a.raw, spec['series'])
    if not os.path.isdir(sdir): print(f'{name} : prise brute absente ({sdir}), ignorée'); continue
    meta = json.load(open(os.path.join(sdir, 'serie.json')))
    segs = meta.get('segments') or []
    pseg = min(range(len(segs)), key=lambda i: (objective_number(segs[i].get('objective')), i)) if segs else 0
    work = os.path.join(a.raw, '_tests', name); os.makedirs(work, exist_ok=True)
    print(f'== {name} ({spec["series"]}) : {len(segs) or 1} segment(s), ensemble = segment {pseg + 1}')
    res = {}
    rc, dt = run(sdir, os.path.join(work, 'ensemble'), ['--segment', str(pseg)] if segs else [])
    if rc != 0: print(f'  ensemble : ÉCHEC (code {rc})'); failed = True; continue
    res['ensemble'] = parse_log(os.path.join(work, 'ensemble', 'pipeline.log')); res['ensemble']['seconds'] = round(dt)
    print(f'  ensemble : {res["ensemble"]}')
    for seg in range(len(segs)):
        if seg == pseg: continue
        od = os.path.join(work, f'detail{seg}')
        os.makedirs(od, exist_ok=True)
        link = os.path.join(od, 'frames')
        if not os.path.islink(link): os.symlink(os.path.abspath(os.path.join(work, 'ensemble', 'frames')), link)
        rc, dt = run(sdir, od, ['--segment', str(seg), '--parent', os.path.join(work, 'ensemble')])
        if rc != 0: print(f'  détail {seg} : ÉCHEC (code {rc})'); failed = True; continue
        r = parse_log(os.path.join(od, 'pipeline.log')); r['seconds'] = round(dt)
        try:
            lay = json.load(open(os.path.join(od, 'layout.json')))
            if lay.get('parent_transform'): r['tx'], r['ty'] = round(lay['parent_transform'][2]), round(lay['parent_transform'][3])
        except Exception: pass
        res[f'detail{seg}'] = r
        print(f'  détail {seg} : {r}')
    results[name] = res
    if not a.record:
        for part, want in (spec.get('expect') or {}).items():
            errs = check(name, res.get(part, {}), want, TOL)
            for e in errs: print(f'  ÉCART {part} {e}')
            if errs: failed = True
    else:
        spec['expect'] = {part: {k: v for k, v in r.items() if k != 'seconds'} for part, r in res.items()}

if a.record:
    os.makedirs(os.path.dirname(REF), exist_ok=True)
    json.dump(ref, open(REF, 'w'), indent=1, ensure_ascii=False)
    print(f'références enregistrées dans {REF}')
print('RÉSULTAT :', 'écarts détectés' if failed else 'conforme aux références')
sys.exit(1 if failed else 0)
