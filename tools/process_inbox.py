#!/usr/bin/env python3
"""Traite les prises deposees dans le bucket Supabase « series » : pour chaque serie close (serie.json present),
telecharge les morceaux video (ou les photos), assemble (tools/pipeline.py), publie (tools/publish_lame.py)
puis supprime la serie du bucket. Lance par GitHub Actions ; utilisable aussi a la main.

Env : SUPABASE_URL, SUPABASE_SERVICE_KEY. Options : --keep (ne pas supprimer du bucket), --only <serie>
"""
import argparse, glob, json, os, re, shutil, subprocess, sys, time
import requests

ap = argparse.ArgumentParser()
ap.add_argument('--keep', action='store_true')
ap.add_argument('--only', default=None)
ap.add_argument('--work', default='inbox')
ap.add_argument('--keep-days', type=float, default=float(os.environ.get('KEEP_DAYS', '60')), help='garder les prises publiees ce nombre de jours (reassemblage possible ; archive brute)')
ap.add_argument('--fetch-only', action='store_true', help='telecharger la prise (avec --only) sans l assembler : diagnostic hors ligne')
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
def git(*args, check=False):
    r = subprocess.run(['git'] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0: raise subprocess.CalledProcessError(r.returncode, args, r.stdout, r.stderr)
    return r
def regen_gallery():
    subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); from publish_lame import write_gallery; write_gallery()', os.path.join(ROOT, 'tools')])
def git_publish(paths, message):
    """Commite les dossiers donnes, se synchronise avec origin/main, regenere la galerie et pousse. Retourne True si pousse."""
    try:
        git('config', 'user.name', 'scanlame'); git('config', 'user.email', 'scanlame@users.noreply.github.com')
        git('add', '-A', '--', 'lames')
        git('reset', '-q', '--', 'lames/index.html', 'lames/lames.json')          # fichiers derives : regeneres apres synchronisation
        git('checkout', '--', 'lames/index.html', 'lames/lames.json')
        if git('diff', '--cached', '--quiet').returncode != 0: git('commit', '-q', '-m', message, check=True)
        for attempt in range(3):
            r = git('pull', '--rebase', '-q', 'origin', 'main')
            if r.returncode != 0:
                # conflit : seuls les fichiers derives de la galerie peuvent entrer en conflit, on les regenere
                regen_gallery(); git('add', '--', 'lames/index.html', 'lames/lames.json')
                env = dict(os.environ, GIT_EDITOR='true')
                rc = subprocess.run(['git', 'rebase', '--continue'], capture_output=True, text=True, env=env).returncode
                if rc != 0: git('rebase', '--abort'); print('  synchronisation git impossible : ' + r.stderr[-400:]); return False
            regen_gallery(); git('add', '--', 'lames/index.html', 'lames/lames.json')
            if git('diff', '--cached', '--quiet').returncode != 0: git('commit', '-q', '-m', 'Galerie', check=True)
            if git('push', '-q', 'origin', 'HEAD:main').returncode == 0:
                print('  poussée vers la galerie'); return True
            time.sleep(5)
        print('  push refusé trois fois'); return False
    except subprocess.CalledProcessError as e:
        print(f'  git : {e.stderr[-400:] if e.stderr else e}'); return False

def release_full(slug, pano):
    """Image entiere de la lame (JPEG pleine resolution) deposee dans la release GitHub « lames » : telechargement direct
    (https://github.com/<repo>/releases/download/lames/<slug>.jpg), pour l'analyse hors ligne (CNN, QuPath...)."""
    if not os.environ.get('GITHUB_ACTIONS') or not os.path.isfile(pano): return
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    try:
        if subprocess.run(['gh', 'release', 'view', 'lames', '-R', repo], capture_output=True).returncode != 0:
            subprocess.run(['gh', 'release', 'create', 'lames', '-R', repo, '--title', 'Lames entières (JPEG pleine résolution)', '--notes', 'Une image par lame, nommée <slug>.jpg, remplacée à chaque réassemblage ou rognage.', '--latest=false'], check=True, capture_output=True)
        tmp = os.path.join(os.path.dirname(pano), f'{slug}.jpg'); shutil.copyfile(pano, tmp)
        zipp = os.path.join(os.path.dirname(pano), f'{slug}-visionneuse.zip')
        subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); from publish_lame import write_offline_zip; write_offline_zip(sys.argv[2], sys.argv[3])', os.path.join(ROOT, 'tools'), slug, zipp])
        r = subprocess.run(['gh', 'release', 'upload', 'lames', tmp] + ([zipp] if os.path.isfile(zipp) else []) + ['-R', repo, '--clobber'], capture_output=True, text=True)
        os.remove(tmp)
        if os.path.isfile(zipp): os.remove(zipp)
        print(f'  image entière ({os.path.getsize(pano) >> 20} Mo) et visionneuse hors ligne déposées : {slug}.jpg, {slug}-visionneuse.zip' if r.returncode == 0 else f'  dépôt refusé : {r.stderr[-200:]}')
    except Exception as e: print(f'  dépôt de l\'image entière impossible : {e}')

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
    # --- demande d'edition venue de la visionneuse (rogner, supprimer) : serie.json avec un champ action, pas de photos ---
    if s.startswith('edit_') and 'serie.json' in names:
        tmpj = os.path.join(a.work, f'{s}.json'); os.makedirs(a.work, exist_ok=True)
        try:
            get(f'{s}/serie.json', tmpj); cmd = json.load(open(tmpj))
            action, slug = cmd.get('action'), re.sub(r'[^a-z0-9-]', '', str(cmd.get('slug') or ''))
            if not slug or not os.path.isfile(os.path.join(ROOT, 'lames', slug, 'lame.json')):
                print(f'{s} : {action} sur lame inconnue « {slug} », ignorée')
            elif action == 'delete':
                print(f'{s} : suppression de {slug}')
                subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'edit_lame.py'), 'delete', slug], check=True)
                if os.environ.get('GITHUB_ACTIONS'):
                    git_publish(['lames'], f'Suppression : {slug}')
                    for asset in (f'{slug}.jpg', f'{slug}-visionneuse.zip'):
                        subprocess.run(['gh', 'release', 'delete-asset', 'lames', asset, '-R', os.environ.get('GITHUB_REPOSITORY', ''), '-y'], capture_output=True)
            elif action == 'crop' and isinstance(cmd.get('rect'), list) and len(cmd['rect']) == 4:
                r_ = [str(int(v)) for v in cmd['rect']]
                print(f'{s} : rognage de {slug} à {r_}')
                subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'edit_lame.py'), 'crop', slug] + r_, check=True)
                if os.environ.get('GITHUB_ACTIONS'):
                    git_publish(['lames'], f'Rognage : {slug}')
                    fullp = os.path.join(a.work, f'{slug}_full.jpg')
                    subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'telecharger_lame.py'), slug, '--local', '-o', fullp])
                    release_full(slug, fullp)
            else:
                print(f'{s} : action inconnue {action}')
        except Exception as e:
            print(f'{s} : échec de l\'édition : {e}')
        rm([f'{s}/serie.json'])
        continue
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
    if 'done.txt' in names and not ((a.force or a.fetch_only) and a.only):
        # published earlier: keep the raw take a few days, then delete it
        from datetime import datetime, timezone, timedelta
        done_at = next((e.get('updated_at') or e.get('created_at') for e in entries if e['name'] == 'done.txt'), None)
        if done_at:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(done_at.replace('Z', '+00:00'))
            if age > timedelta(days=a.keep_days):
                allp = [f'{s}/{e["name"]}' for e in entries if e.get('id')] + [f'{s}/chunks/{e["name"]}' for e in ls(f'{s}/chunks')] + [f'{s}/photos/{e["name"]}' for e in ls(f'{s}/photos')]
                rm(allp); print(f'{s} : publiée il y a {age.days} j, prise supprimée du bucket'); continue
        print(f'{s} : déjà publiée, conservée'); continue
    if 'failed.txt' in names and not ((a.force or a.fetch_only) and a.only):
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
    if a.fetch_only: print('  téléchargée seulement (--fetch-only)'); continue
    if (not chunks and not photos) or (vid and os.path.getsize(vid) < 200_000):
        print('  vide ou trop courte : supprimée'); rm(paths); continue
    t0 = time.time()
    res = os.path.join(d, 'result')
    PARGS = os.environ.get('PIPELINE_ARGS', '').split()
    PAR0 = ['--parent', meta['parent']] if meta.get('parent') and os.path.isfile(os.path.join(ROOT, 'lames', meta['parent'], 'lame.json')) else []
    rc = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'pipeline.py'), d, '--out', res] + PAR0 + PARGS).returncode
    if rc != 0:
        print(f'  ÉCHEC du pipeline (code {rc}), série marquée en échec dans le bucket')
        logp = os.path.join(res, 'pipeline.log')
        put_text(f'{s}/failed.txt', open(logp).read()[-4000:] if os.path.isfile(logp) else f'pipeline code {rc}')
        # journaux et planche-contact de l'echec publies dans lames/echecs/<serie>/ pour diagnostic
        ed = os.path.join(ROOT, 'lames', 'echecs', s); os.makedirs(ed, exist_ok=True)
        for src, dst in ((logp, 'assemblage.txt'), (os.path.join(d, 'journal-telephone.txt'), 'telephone.txt'), (os.path.join(res, 'planche.jpg'), 'planche.jpg')):
            if os.path.isfile(src): shutil.copyfile(src, os.path.join(ed, dst))
        if os.environ.get('GITHUB_ACTIONS'): git_publish([f'lames/echecs/{s}'], f'Échec : {s}')
        continue
    # --- segments d'objectif (changement d'objectif pendant la prise) et zones distinctes (composantes) ---
    def run_pipeline(outdir, extra):
        os.makedirs(outdir, exist_ok=True)
        link = os.path.join(outdir, 'frames')
        if os.path.islink(link) and not os.path.isdir(link): os.unlink(link)
        if not os.path.isdir(link): os.symlink(os.path.abspath(os.path.join(res, 'frames')), link)
        return subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'pipeline.py'), d, '--out', outdir] + extra + os.environ.get('PIPELINE_ARGS', '').split()).returncode == 0
    def layout_of(r):
        try: return json.load(open(os.path.join(r, 'layout.json')))
        except Exception: return {}
    lay0 = layout_of(res)
    nseg = int(lay0.get('segments') or 0)
    # jobs : (dossier result, numero de zone ou None, numero de segment, parent explicite ou None)
    jobs = [(res, None, 0, None)]
    def add_zones(r, seg):
        others = layout_of(r).get('zone_components') or []      # numeros reels des composantes a assembler a part
        for n_, comp_i in enumerate(others, start=1):
            rc_ = os.path.join(d, f'result_s{seg}_c{n_}')
            if run_pipeline(rc_, ['--segment', str(seg), '--component', str(comp_i)] + (['--parent', res] if seg > 0 else PAR0)): jobs.append((rc_, n_, seg, None))
            else: print(f'  segment {seg + 1} zone {ci + 1} : échec de l\'assemblage, ignorée')
    if meta.get('parent'): add_zones(res, 0)
    for seg in range(1, nseg):
        rs = os.path.join(d, f'result_s{seg}')
        if run_pipeline(rs, ['--segment', str(seg), '--parent', res]):   # segment 0 (premier objectif) = lame d'ensemble : les photos y sont ancrées
            jobs.append((rs, None, seg, 'SEG0')); add_zones(rs, seg)
        else: print(f'  segment {seg + 1} : échec de l\'assemblage, ignoré')
    # les zones des segments > 0 heritent aussi du parent SEG0
    jobs = [(r, ci, seg, ('SEG0' if seg > 0 else par)) for (r, ci, seg, par) in jobs]
    if len(jobs) > 1: print(f'  {len(jobs)} lame(s) dans cette prise (segments et zones)')
    base_title = meta.get('title') or s
    published = []; published_ok = True
    seg0_slug = None
    for res, ci, seg, par in jobs:
      lay = layout_of(res)
      title = base_title + (f' (obj. {lay.get("objective")})' if seg > 0 else '') + ('' if ci is None else f' (zone {ci + 1})')
      slug = (slugify(title) + '-' + s.split('_')[1][4:] + s.split('_')[2][:4]) if meta.get('title') and s.count('_') >= 2 else s.lower().replace('_', '-') + ('' if seg == 0 else f'-obj{seg}') + ('' if ci is None else f'-zone{ci + 1}')
      parent = seg0_slug if par == 'SEG0' else (meta.get('parent') if seg == 0 else None)
      if parent:
          lay['parent'] = parent; json.dump(lay, open(os.path.join(res, 'layout.json'), 'w'))
      # relance d'une prise deja publiee (--force) : nouvelle version a cote de l'ancienne, pour comparer les reglages
      version = 1
      if a.force and os.path.isdir(os.path.join(ROOT, 'lames', slug)) and not meta.get('complete'):
          existing = []
          for dn in os.listdir(os.path.join(ROOT, 'lames')):
              lp = os.path.join(ROOT, 'lames', dn, 'lame.json')
              if os.path.isfile(lp):
                  try:
                      lj = json.load(open(lp, encoding='utf-8'))
                      if lj.get('series') == s and lj.get('segment', 0) == seg and (lj.get('zone') or None) == ci: existing.append(int(lj.get('version') or 1))
                  except Exception: pass
          version = (max(existing) if existing else 1) + 1
          slug = f'{slug}-v{version}'; title = f'{title} (v{version})'
          print(f'  version {version} de cette prise (l\'ancienne reste en ligne pour comparaison)')
      lay['series'] = s; lay['version'] = version; lay['segment'] = seg; lay['zone'] = ci
      json.dump(lay, open(os.path.join(res, 'layout.json'), 'w'))
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
      release_full(slug, os.path.join(res, 'pano.jpg'))
      # journaux publies a cote de la lame (telephone + assemblage), le temps de la mise au point
      logs = os.path.join(ROOT, 'lames', slug, 'journaux'); os.makedirs(logs, exist_ok=True)
      stampf = s.replace('lame_', '')
      base_res = res if not res.endswith('merged') else os.path.dirname(res)
      for src, dst in ((os.path.join(d, 'journal-telephone.txt'), f'{stampf}-telephone.txt'), (os.path.join(base_res, 'pipeline.log'), f'{stampf}-assemblage.txt'), (os.path.join(base_res, 'planche.jpg'), f'{stampf}-planche.jpg')):
          if os.path.isfile(src): shutil.copyfile(src, os.path.join(logs, dst))
      subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); from publish_lame import write_pages; write_pages(sys.argv[2])', os.path.join(ROOT, 'tools'), slug])
      if seg == 0 and ci is None: seg0_slug = slug
      if parent:
          if os.path.isfile(os.path.join(ROOT, 'lames', parent, 'lame.json')):
              print(f'  lame de détail de {parent} : recalage dans la lame d\'ensemble')
              subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'link_child.py'), parent, slug])
          else:
              print(f'  lame parente {parent} introuvable, publiée seule')
      # details publies avant leur lame d'ensemble (prise x100 envoyee avant que la x10 soit assemblee) : rattaches maintenant
      extra_paths = []
      for cdir in sorted(glob.glob(os.path.join(ROOT, 'lames', '*', 'lame.json'))):
          try: cj = json.load(open(cdir))
          except Exception: continue
          cslug = os.path.basename(os.path.dirname(cdir))
          if cslug != slug and cj.get('parent') == slug and not cj.get('parent_transform'):
              print(f'  détail en attente {cslug} : recalage dans {slug}')
              subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'link_child.py'), slug, cslug])
              extra_paths.append(f'lames/{cslug}')
      # sous GitHub Actions : publication immediate de cette lame. Seul son dossier est commite ; la galerie
      # (fichiers derives) est regeneree apres synchronisation pour ne jamais entrer en conflit.
      pushed = git_publish([f'lames/{slug}'] + ([f'lames/{parent}'] if parent and os.path.isdir(os.path.join(ROOT, 'lames', parent)) else []) + extra_paths, f'Lame : {slug}') if os.environ.get('GITHUB_ACTIONS') else True
      if not pushed: print('  publication immédiate impossible, la prise reste à traiter au prochain passage'); published_ok = False
      print(f'  publiée : lames/{slug}/ en {time.time() - t0:.0f} s')
      if published_ok: published.append(slug)
    if published and published_ok:
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
