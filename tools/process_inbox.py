#!/usr/bin/env python3
"""Traite les prises deposees dans le bucket Supabase « series » : pour chaque serie close (serie.json present),
telecharge les morceaux video (ou les photos), assemble (tools/pipeline.py), publie (tools/publish_lame.py)
puis supprime la serie du bucket. Lance par GitHub Actions ; utilisable aussi a la main.

Env : SUPABASE_URL, SUPABASE_SERVICE_KEY. Options : --keep (ne pas supprimer du bucket), --only <serie>
"""
import argparse, json, os, shutil, subprocess, sys, time
import requests

ap = argparse.ArgumentParser()
ap.add_argument('--keep', action='store_true')
ap.add_argument('--only', default=None)
ap.add_argument('--work', default='inbox')
ap.add_argument('--keep-days', type=float, default=float(os.environ.get('KEEP_DAYS', '60')), help='garder les prises publiees ce nombre de jours (reassemblage possible ; archive brute)')
ap.add_argument('--force', action='store_true', help='retraiter meme une serie deja publiee ou en echec (avec --only)')
ap.add_argument('--storage-limit-mb', type=float, default=float(os.environ.get('STORAGE_LIMIT_MB', '80000')), help='au-dela, les prises publiees les plus anciennes sont supprimees sans attendre --keep-days')
a = ap.parse_args()

URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
if not URL or not KEY:
    sys.exit('SUPABASE_URL / SUPABASE_SERVICE_KEY manquants')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}
BUCKET = 'series'

def ls(prefix):
    r = requests.post(f'{URL}/storage/v1/object/list/{BUCKET}', headers=H, json={'prefix': prefix, 'limit': 1000, 'sortBy': {'column': 'name', 'order': 'asc'}})
    r.raise_for_status(); return r.json()
def get(path, dest):
    with requests.get(f'{URL}/storage/v1/object/{BUCKET}/{path}', headers=H, stream=True) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for c in r.iter_content(1 << 20): f.write(c)
def put_text(path, text):
    r = requests.post(f'{URL}/storage/v1/object/{BUCKET}/{path}', headers={**H, 'Content-Type': 'text/plain', 'x-upsert': 'true'}, data=text.encode()); r.raise_for_status()
def slugify(t):
    import unicodedata, re
    t = unicodedata.normalize('NFKD', t).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', t).strip('-')[:40]
def rm(paths):
    for i in range(0, len(paths), 100):
        r = requests.delete(f'{URL}/storage/v1/object/{BUCKET}', headers=H, json={'prefixes': paths[i:i + 100]}); r.raise_for_status()

done = []
seen = set()
def pending_series():
    names = [e['name'] for e in ls('') if e.get('id') is None or e.get('metadata') is None]   # dossiers
    return [n for n in names if (not a.only or n == a.only) and n not in seen]
series = []
while True:
  if not series:
      series = pending_series()   # relu a chaque tour : une prise arrivee pendant le passage est traitee tout de suite
      if not series: break
      if seen: print(f'nouvelle(s) série(s) arrivée(s) pendant le passage : {series}')
  s = series.pop(0); seen.add(s)
  if True:
    entries = ls(s)
    names = {e['name'] for e in entries}
    orphan = False
    if 'serie.json' not in names:
        # recording never closed (page reloaded, double tap...): process it anyway once it has been quiet for 10 min
        from datetime import datetime, timezone, timedelta
        objs = ls(f'{s}/chunks') + ls(f'{s}/photos')
        stamps = [o.get('updated_at') or o.get('created_at') for o in objs if o.get('updated_at') or o.get('created_at')]
        if not stamps:
            print(f'{s} : pas encore close, ignorée'); continue
        newest = max(datetime.fromisoformat(t.replace('Z', '+00:00')) for t in stamps)
        if newest.tzinfo is None: newest = newest.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - newest
        if age < timedelta(minutes=5):
            print(f'{s} : pas encore close (dernier morceau il y a {int(age.total_seconds())} s), ignorée'); continue
        orphan = True
        print(f'{s} : non close depuis plus de 10 min, traitée sans positions')
    if 'done.txt' in names and not (a.force and a.only):
        # published earlier: keep the raw take a few days, then delete it
        from datetime import datetime, timezone, timedelta
        done_at = next((e.get('updated_at') or e.get('created_at') for e in entries if e['name'] == 'done.txt'), None)
        if done_at:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(done_at.replace('Z', '+00:00'))
            if age > timedelta(days=a.keep_days):
                allp = [f'{s}/{e["name"]}' for e in entries if e.get('id')] + [f'{s}/chunks/{e["name"]}' for e in ls(f'{s}/chunks')] + [f'{s}/photos/{e["name"]}' for e in ls(f'{s}/photos')]
                rm(allp); print(f'{s} : publiée il y a {age.days} j, prise supprimée du bucket'); continue
        print(f'{s} : déjà publiée, conservée'); continue
    if 'failed.txt' in names and not (a.force and a.only):
        print(f'{s} : déjà en échec, ignorée'); continue
    print(f'== {s}')
    d = os.path.join(a.work, s); shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
    if orphan:
        meta = {'series': s, 'mode': 'video', 'video_mime': 'video/mp4', 'frames': []}
        json.dump(meta, open(os.path.join(d, 'serie.json'), 'w')); paths = []
    else:
        get(f'{s}/serie.json', os.path.join(d, 'serie.json'))
        meta = json.load(open(os.path.join(d, 'serie.json')))
        paths = [f'{s}/serie.json']
    chunks = sorted(e['name'] for e in ls(f'{s}/chunks'))
    if chunks:
        ext = 'mp4' if 'mp4' in str(meta.get('video_mime', '')) else 'webm'
        with open(os.path.join(d, f'video.{ext}'), 'wb') as out:
            for c in chunks:
                tmp = os.path.join(d, 'tmp.bin'); get(f'{s}/chunks/{c}', tmp)
                with open(tmp, 'rb') as f: shutil.copyfileobj(f, out)
                paths.append(f'{s}/chunks/{c}')
        os.remove(tmp)
        print(f'  vidéo : {len(chunks)} morceaux, {os.path.getsize(os.path.join(d, f"video.{ext}")) >> 20} Mo')
    photos = sorted(e['name'] for e in ls(f'{s}/photos'))
    if photos:
        os.makedirs(os.path.join(d, 'photos'))
        for p in photos:
            get(f'{s}/photos/{p}', os.path.join(d, 'photos', p)); paths.append(f'{s}/photos/{p}')
        print(f'  photos : {len(photos)}')
    if meta.get('journal'):
        open(os.path.join(d, 'journal-telephone.txt'), 'w', encoding='utf-8').write(meta['journal'])
        tail = meta['journal'].strip().split('\n')
        print(f'  journal du téléphone : {len(tail)} lignes, dernières :'); print('\n'.join('    ' + l for l in tail[-12:]))
        meta['journal'] = None; json.dump(meta, open(os.path.join(d, 'serie.json'), 'w'))
    vid = os.path.join(d, f'video.{ext}') if chunks else None
    if (not chunks and not photos) or (vid and os.path.getsize(vid) < 200_000):
        print('  vide ou trop courte : supprimée'); rm(paths); continue
    t0 = time.time()
    res = os.path.join(d, 'result')
    rc = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'pipeline.py'), d, '--out', res]).returncode
    if rc != 0:
        print(f'  ÉCHEC du pipeline (code {rc}), série marquée en échec dans le bucket')
        logp = os.path.join(res, 'pipeline.log')
        put_text(f'{s}/failed.txt', open(logp).read()[-4000:] if os.path.isfile(logp) else f'pipeline code {rc}'); continue
    # --- segments d'objectif (changement d'objectif pendant la prise) et zones distinctes (composantes) ---
    def run_pipeline(outdir, extra):
        os.makedirs(outdir, exist_ok=True)
        link = os.path.join(outdir, 'frames')
        if os.path.islink(link) and not os.path.isdir(link): os.unlink(link)
        if not os.path.isdir(link): os.symlink(os.path.abspath(os.path.join(res, 'frames')), link)
        return subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'pipeline.py'), d, '--out', outdir] + extra).returncode == 0
    def layout_of(r):
        try: return json.load(open(os.path.join(r, 'layout.json')))
        except Exception: return {}
    lay0 = layout_of(res)
    nseg = int(lay0.get('segments') or 0)
    # jobs : (dossier result, numero de zone ou None, numero de segment, parent explicite ou None)
    jobs = [(res, None, 0, None)]
    def add_zones(r, seg):
        others = layout_of(r).get('other_components') or []
        for ci in range(1, 1 + len(others)):
            rc_ = os.path.join(d, f'result_s{seg}_c{ci}')
            if run_pipeline(rc_, ['--segment', str(seg), '--component', str(ci)]): jobs.append((rc_, ci, seg, None))
            else: print(f'  segment {seg + 1} zone {ci + 1} : échec de l\'assemblage, ignorée')
    if meta.get('parent'): add_zones(res, 0)
    for seg in range(1, nseg):
        rs = os.path.join(d, f'result_s{seg}')
        if run_pipeline(rs, ['--segment', str(seg)]):
            jobs.append((rs, None, seg, 'SEG0')); add_zones(rs, seg)
        else: print(f'  segment {seg + 1} : échec de l\'assemblage, ignoré')
    # les zones des segments > 0 heritent aussi du parent SEG0
    jobs = [(r, ci, seg, ('SEG0' if seg > 0 else par)) for (r, ci, seg, par) in jobs]
    if len(jobs) > 1: print(f'  {len(jobs)} lame(s) dans cette prise (segments et zones)')
    base_title = meta.get('title') or s
    published = []
    seg0_slug = None
    for res, ci, seg, par in jobs:
      lay = layout_of(res)
      title = base_title + (f' (obj. {lay.get("objective")})' if seg > 0 else '') + ('' if ci is None else f' (zone {ci + 1})')
      slug = (slugify(title) + '-' + s.split('_')[1][4:] + s.split('_')[2][:4]) if meta.get('title') and s.count('_') >= 2 else s.lower().replace('_', '-') + ('' if seg == 0 else f'-obj{seg}') + ('' if ci is None else f'-zone{ci + 1}')
      parent = seg0_slug if par == 'SEG0' else (meta.get('parent') if seg == 0 else None)
      if parent:
          lay['parent'] = parent; json.dump(lay, open(os.path.join(res, 'layout.json'), 'w'))
      complete = meta.get('complete') if (seg == 0 and ci is None) else None
      if complete and os.path.isfile(os.path.join(ROOT, 'lames', complete, 'lame.json')):
          # reprise ciblee : la prise est fondue dans la lame existante, qui garde son nom
          print(f'  reprise de {complete} : fusion dans la lame existante')
          rc = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'merge_retake.py'), complete, res]).returncode
          if rc != 0:
              print('  ÉCHEC de la fusion : la reprise ne se recale pas dans la lame, publiée à part')
              title = f'{title} (reprise non recalée)'
          else:
              slug, res, title = complete, os.path.join(res, 'merged'), None
      elif complete:
          print(f'  lame à compléter {complete} introuvable : publiée à part')
      cmd = [sys.executable, os.path.join(ROOT, 'tools', 'publish_lame.py'), os.path.join(res, 'pano.jpg'), '--name', slug,
             '--fields', os.path.join(res, 'fields.json'), '--layout', os.path.join(res, 'layout.json')] + (['--title', title] if title else [])
      rc = subprocess.run(cmd).returncode
      if rc != 0:
          print('  ÉCHEC de la publication'); put_text(f'{s}/failed.txt', 'publication'); break
      # journaux publies a cote de la lame (telephone + assemblage), le temps de la mise au point
      logs = os.path.join(ROOT, 'lames', slug, 'journaux'); os.makedirs(logs, exist_ok=True)
      stampf = s.replace('lame_', '')
      for src, dst in ((os.path.join(d, 'journal-telephone.txt'), f'{stampf}-telephone.txt'), (os.path.join(res if not res.endswith('merged') else os.path.dirname(res), 'pipeline.log'), f'{stampf}-assemblage.txt')):
          if os.path.isfile(src): shutil.copyfile(src, os.path.join(logs, dst))
      subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); from publish_lame import write_pages; write_pages(sys.argv[2])', os.path.join(ROOT, 'tools'), slug])
      if seg == 0 and ci is None: seg0_slug = slug
      if parent:
          if os.path.isfile(os.path.join(ROOT, 'lames', parent, 'lame.json')):
              print(f'  lame de détail de {parent} : recalage dans la lame d\'ensemble')
              subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'link_child.py'), parent, slug])
          else:
              print(f'  lame parente {parent} introuvable, publiée seule')
      print(f'  publiée : lames/{slug}/ en {time.time() - t0:.0f} s')
      published.append(slug)
      # sous GitHub Actions : publication immediate de cette lame (la galerie n'attend pas la fin du passage)
      if os.environ.get('GITHUB_ACTIONS'):
          try:
              subprocess.run(['git', 'config', 'user.name', 'scanlame'], check=True)
              subprocess.run(['git', 'config', 'user.email', 'scanlame@users.noreply.github.com'], check=True)
              subprocess.run(['git', 'add', 'lames'], check=True)
              if subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode != 0:
                  subprocess.run(['git', 'commit', '-q', '-m', f'Lame : {slug}'], check=True)
                  subprocess.run(['git', 'pull', '--rebase', '-q', 'origin', 'main'], check=True)
                  subprocess.run(['git', 'push', '-q', 'origin', 'HEAD:main'], check=True)
                  print('  poussée vers la galerie')
          except subprocess.CalledProcessError as e:
              print(f'  publication immédiate impossible ({e}), elle partira en fin de passage')
    if published:
        put_text(f'{s}/done.txt', ','.join(published))
        if 'failed.txt' in names: rm([f'{s}/failed.txt'])
        if not a.keep and a.keep_days <= 0: rm(paths)
        done.extend(published)
    shutil.rmtree(d, ignore_errors=True)
# --- plafond de stockage : le bucket gratuit fait 1 Go ; au-dela du plafond on supprime les prises deja publiees, les plus anciennes d'abord ---
def series_files(sname):
    out = [(f'{sname}/{e["name"]}', e) for e in ls(sname) if e.get('id')]
    out += [(f'{sname}/chunks/{e["name"]}', e) for e in ls(f'{sname}/chunks')]
    out += [(f'{sname}/photos/{e["name"]}', e) for e in ls(f'{sname}/photos')]
    return out
inv = []
for sname in [e['name'] for e in ls('') if e.get('id') is None]:
    files = series_files(sname)
    size = sum((e.get('metadata') or {}).get('size', 0) for _, e in files)
    inv.append((sname, size, any(p.endswith('/done.txt') for p, _ in files), [p for p, _ in files]))
total = sum(x[1] for x in inv)
print(f'stockage : {total / 1048576:.0f} Mo dans le bucket ({len(inv)} série(s))')
for sname, size, is_done, paths in sorted(inv):   # noms horodates : ordre chronologique
    if total <= a.storage_limit_mb * 1048576: break
    if not is_done: continue
    rm(paths); total -= size
    print(f'{sname} : supprimée pour libérer de la place ({size / 1048576:.0f} Mo), reste {total / 1048576:.0f} Mo')
print('séries publiées :', done or 'aucune')
if done:
    open(os.environ.get('GITHUB_OUTPUT', '/dev/null'), 'a').write('published=' + ','.join(done) + '\n')
