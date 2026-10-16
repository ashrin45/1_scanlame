# ScanLame, application Android

Application native de capture pour les lames virtuelles : même format d'échange que la page web
(`serie.json`, photos ou morceaux de vidéo, positions du suivi), même fonction Supabase, même pipeline.

Ce qu'elle apporte par rapport à la page : choix de la caméra physique (sans bascule automatique
d'objectif), mise au point, exposition, ISO et balance des blancs tenus, photos pleine définition à chaque
arrêt de platine ou vidéo 4K, envoi en arrière-plan.

## Installation

L'APK est compilé par GitHub Actions à chaque modification de `android/` et publié dans la release
« android » du dépôt : https://github.com/ashrin45/1_scanlame/releases/tag/android

Sur le téléphone : télécharger `scanlame.apk`, l'ouvrir, autoriser l'installation depuis cette source.
Les mises à jour s'installent par-dessus si la clé de signature est la même (secrets GitHub
`SCANLAME_KEYSTORE_B64` et `SCANLAME_KEYSTORE_PASS`) ; sinon il faut désinstaller avant.

## Premier lancement

1. Réglages : URL de la fonction Supabase et jeton (les mêmes que dans la page).
2. Diagnostic : liste des caméras et de leurs capacités réelles (à copier dans la conversation pour choisir
   l'objectif physique et les plages de réglage).
3. Capture : nom de la lame, coloration, objectif, puis « Lancer la série ».

## Compilation locale

```
cd android && ./gradlew assembleDebug
```
