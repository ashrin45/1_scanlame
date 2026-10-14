# Scan lame

Capture de champs microscopiques depuis un smartphone pose sur l'oculaire, avec suivi de position en direct
et carte de couverture. Les images sont assemblees ensuite en lame virtuelle.

Page (Chrome Android, HTTPS) : https://ashrin45.github.io/1_scanlame/

## Deux modes

- **Video (par defaut)** : enregistrement par l'encodeur materiel du telephone a 60 Mbit/s, cadence parfaite,
  aucune charge CPU. Les positions sont echantillonnees toutes les 250 ms depuis l'apercu et horodatees.
  Les images sont extraites ensuite : `python tools/extract_frames.py lame_XXXX.zip --fps 5`
  (ffmpeg + OpenCV ; rejette les images floues et associe les positions).
- **Photos JPEG a intervalle** : une photo toutes les 0,2 a 1 s, encodage dans des workers paralleles.
  Utile pour la pleine resolution du capteur, au prix d'une cadence qui depend du telephone.

## Usage

Demarrer la camera, choisir le flux (moyen suffit pour commencer), mise au point a la bague du microscope
en surveillant la jauge de nettete, verrouiller AF/AE, lancer la serie, deplacer la platine lentement en
serpentin en gardant le voyant de suivi vert, arreter, telecharger le ZIP (video ou photos, serie.json avec
les positions, journal.txt).
