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

## Recevoir les prises sur son PC depuis n'importe quel reseau (gratuit)

1. Telecharger cloudflared.exe (Cloudflare Zero Trust downloads), le poser a cote de tools/server.py.
2. Terminal 1 : `python tools\server.py --root D:\lames`
3. Terminal 2 : `cloudflared tunnel --url http://localhost:8000` -> URL https://xxxx.trycloudflare.com
4. Coller cette URL dans « URL du serveur » sur la page. La video arrive seconde par seconde dans D:\lames\<serie>\.

L'URL change a chaque lancement ; un compte Cloudflare gratuit + un domaine la rend fixe.
Alternative : Tailscale Funnel (URL stable, app Tailscale sur le telephone et le PC).
Pour un serveur allume en permanence sans frais : Oracle Cloud Always Free (ARM 4 coeurs, 24 Go, 200 Go de disque).
Installation en une commande sur la VM, voir tools/oracle/setup.sh (Caddy + HTTPS Let's Encrypt via DuckDNS,
service systemd, dossier /data/series pour les prises et /data/lames pour les pyramides publiees).
Le serveur demande un jeton (X-Token) ; le meme jeton se saisit dans la page.

## Usage

Demarrer la camera, choisir le flux (moyen suffit pour commencer), mise au point a la bague du microscope
en surveillant la jauge de nettete, verrouiller AF/AE, lancer la serie, deplacer la platine lentement en
serpentin en gardant le voyant de suivi vert, arreter, telecharger le ZIP (video ou photos, serie.json avec
les positions, journal.txt).
