#!/usr/bin/env python3
"""Extrait les images d'une serie video enregistree par la page de capture.

Usage : python extract_frames.py lame_XXXX.zip [--fps 5] [--blur 40] [--out dossier]

- decompresse le ZIP (video + serie.json + journal.txt)
- extrait les images a la cadence demandee avec ffmpeg
- rejette les images floues (variance du laplacien sous le seuil)
- associe a chaque image la position estimee la plus proche en temps (serie.json)
- ecrit frames/*.jpg et frames.json (nom, t_ms, x, y, score, nettete)
"""
import argparse, json, os, subprocess, sys, zipfile, glob
import cv2, numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('zip')
ap.add_argument('--fps', type=float, default=5.0)
ap.add_argument('--blur', type=float, default=40.0, help='seuil de nettete (variance du laplacien)')
ap.add_argument('--out', default=None)
a = ap.parse_args()

out = a.out or os.path.splitext(os.path.basename(a.zip))[0]
os.makedirs(out, exist_ok=True)
with zipfile.ZipFile(a.zip) as z:
    z.extractall(out)
meta = json.load(open(os.path.join(out, 'serie.json')))
videos = [f for f in os.listdir(out) if f.endswith(('.mp4', '.webm'))]
if not videos:
    sys.exit('pas de video dans le ZIP (mode photos ?)')
video = os.path.join(out, videos[0])
frames_dir = os.path.join(out, 'frames')
os.makedirs(frames_dir, exist_ok=True)

# ffmpeg : une image toutes les 1/fps s, horodatage dans le nom via -frame_pts
subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', video, '-vf', f'fps={a.fps}', '-q:v', '2',
                '-frame_pts', '1', os.path.join(frames_dir, 'f_%08d.jpg')], check=True)

samples = [f for f in meta.get('frames', []) if f.get('kind') == 'sample' and f.get('x') is not None]
samples.sort(key=lambda f: f['t_ms'])
ts = np.array([f['t_ms'] for f in samples]) if samples else np.array([])

rows, kept, dropped = [], 0, 0
for path in sorted(glob.glob(os.path.join(frames_dir, 'f_*.jpg'))):
    pts = int(os.path.basename(path)[2:-4])            # index d'image dans le flux a `fps`
    t_ms = pts * 1000.0 / a.fps
    im = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    sharp = float(cv2.Laplacian(cv2.resize(im, None, fx=0.25, fy=0.25), cv2.CV_64F).var())
    if sharp < a.blur:
        os.remove(path); dropped += 1; continue
    pos = None
    if len(ts):
        k = int(np.argmin(np.abs(ts - t_ms)))
        if abs(ts[k] - t_ms) < 600:
            pos = samples[k]
    rows.append(dict(name=os.path.basename(path), t_ms=round(t_ms), sharp=round(sharp, 1),
                     x=pos and pos['x'], y=pos and pos['y'], score=pos and pos.get('score'), track=pos and pos.get('track')))
    kept += 1
json.dump(dict(video=videos[0], fps=a.fps, width=meta.get('width'), height=meta.get('height'), frames=rows),
          open(os.path.join(out, 'frames.json'), 'w'), indent=1)
print(f'{kept} images gardees, {dropped} floues rejetees -> {frames_dir}')
