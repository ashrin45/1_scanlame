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

## Stockage

Une prise brute pese vite plusieurs centaines de Mo (30 Mbit/s = 225 Mo par minute). Elle ne doit pas etre
conservee : la lame finie (pyramide de tuiles JPEG) pese 5 a 10 fois moins que la somme des images.
Chaine conseillee : telephone -> serveur (tools/server.py, envoi au fil de l'eau) -> extraction des images
nettes (tools/extract_frames.py) -> stitching -> pyramide de tuiles ; puis suppression de la video et des images.
Debit video reglable dans la page (15 / 30 / 60 Mbit/s) ; 30 suffit pour un Gram a 2000 px.

## Chaine tout en ligne, gratuite (recommandee)

telephone -> Supabase Storage (fonction tools/supabase/scanlame) -> GitHub Actions (toutes les 15 min :
tools/process_inbox.py = extraction + assemblage + publication) -> galerie GitHub Pages (lames/).

Mise en place, une fois :
1. Supabase : Edge Functions > nouvelle fonction « scanlame », coller tools/supabase/scanlame/index.ts,
   desactiver « Enforce JWT verification », secret SCANLAME_TOKEN = jeton de la page.
2. GitHub (ce depot) : secrets SUPABASE_URL (https://xxxx.supabase.co) et SUPABASE_SERVICE_KEY (cle service_role).
3. Page de capture : URL du serveur = https://xxxx.supabase.co/functions/v1/scanlame, jeton = SCANLAME_TOKEN.
Le bucket « series » est cree automatiquement. Les prises sont supprimees du bucket une fois publiees.
Onglet Actions > « Assemblage des prises » > Run workflow pour lancer sans attendre le prochain quart d'heure.

## Assemblage (tools/pipeline.py)

Extraction a 4 im/s, selection des images nettes et suffisamment deplacees, fusion de profondeur (EDF) sur les
arrets de platine (tourner la vis micrometrique sans bouger : les plans nets sont fusionnes), flat-field mesure
si la prise commence par 2 s de champ vide (sinon estime), correction chromatique radiale et distorsion de
lentille (k1) estimees sur les points apparies, recalage global de toutes les images en meme temps (ajustement
par moindres carres robustes), compensation photometrique entre images, composition par tuiles avec coeur du
champ privilegie, coutures et fondu multibande. Dependances : ffmpeg, opencv-python-headless, numpy, scipy.

Barre d'echelle : saisir dans les reglages de la page « µm par pixel du flux », mesure une fois avec un
micrometre objet (ex. 100 µm sur l'image = 1180 px du flux -> 0.085). La valeur est propre a l'objectif, au
reglage de flux et au zoom numerique ; la visionneuse affiche alors une barre (µm, mm) qui suit le zoom.

## Metadonnees, qualite, profondeur, grossissements lies, file d'attente

- Reglages de la page : coloration, objectif, prelevement, commentaire, lame d'ensemble. Coloration et objectif sont
  memorises ; la calibration « µm par pixel » l'est par objectif. Tout est repris dans la galerie (filtres par
  coloration et objectif) et dans la visionneuse.
- Rapport de qualite (bouton « Qualite » de la visionneuse, note A/B/C) : trous dans la couverture, champs peu nets,
  precision du recalage, images floues rejetees, surface couverte. Le calque colore chaque champ selon sa nettete
  et entoure les trous : on sait quoi refilmer sans ouvrir toute la lame.
- Profondeur navigable : sur chaque arret de platine avec balayage de la vis micrometrique, jusqu'a 5 plans sont
  conserves en plus de la fusion EDF. Le curseur « profondeur » de la visionneuse (ou les touches [ et ]) passe
  d'un plan a l'autre, la position 0 etant l'image nette partout. Seules les tuiles qui changent sont stockees.
- Grossissements lies : filmer d'abord une vue d'ensemble (x10), puis des details (x40, x100) en choisissant la
  lame d'ensemble dans les reglages. Le detail est recale par points dans la mosaique d'ensemble (tools/link_child.py)
  et s'affiche en calque a sa place dans la visionneuse parente (bouton « Details ») ; sans recalage possible il
  reste accessible par lien.
- File d'attente hors ligne : si le reseau tombe, les morceaux et la cloture sont gardes dans le telephone
  (IndexedDB) et repartent seuls au retour du reseau, au chargement de la page ou avec « Envoyer maintenant ».

## Alternative : recevoir sur son PC (tools/server.py)

`python tools\server.py --root D:\lames --token JETON` puis `cloudflared tunnel --url http://localhost:8000`
pour une URL HTTPS temporaire ; URL du serveur = cette URL. Assemblage local : `python tools/pipeline.py <serie>`
puis `python tools/publish_lame.py <serie>/result/pano.jpg --name ... --fields <serie>/result/fields.json` et git push.

## Usage

Demarrer la camera, choisir le flux (moyen suffit pour commencer), mise au point a la bague du microscope
en surveillant la jauge de nettete, verrouiller AF/AE, lancer la serie, deplacer la platine lentement en
serpentin en gardant le voyant de suivi vert, arreter, telecharger le ZIP (video ou photos, serie.json avec
les positions, journal.txt).
