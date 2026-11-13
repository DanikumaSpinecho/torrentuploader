# Signature du code avec SignPath Foundation — marche à suivre

Tout ce qui pouvait être préparé dans le dépôt l'est déjà :

| Élément | Où |
|---|---|
| Construction sur GitHub Actions (exigée : SignPath vérifie l'origine du binaire) | `.github/workflows/release.yml` |
| Configuration d'artefact à coller dans SignPath | `.signpath/artifact-configuration.xml` |
| Politique de signature et de confidentialité (exigée) | section « Code signing policy » du `README.md` |
| Licence open source reconnue par l'OSI (exigée) | `LICENSE` (GPL-3.0) |

Tant que SignPath n'est pas configuré, la chaîne publie l'exécutable **non signé** : elle fonctionne
dès maintenant.

## 1. Candidater (à votre nom)

1. Formulaire : <https://signpath.org/apply> (programme gratuit pour l'open source).
2. Réponses suggérées (en anglais) :
   - **Project name**: Torrent Uploader
   - **Repository**: https://github.com/DanikumaSpinecho/torrentuploader
   - **License**: GPL-3.0
   - **Description**: Windows desktop app that sends .torrent files and magnet links to a
     self-hosted ruTorrent/rTorrent seedbox, with Windows file/magnet integration and a companion
     Chrome/Edge extension (native messaging).
   - **Build**: built and released by GitHub Actions (`.github/workflows/release.yml`) with
     PyInstaller; one artifact, `TorrentUploader.exe`.
   - **Code signing policy URL**: https://github.com/DanikumaSpinecho/torrentuploader#code-signing-policy
3. Conditions habituelles du programme : projet maintenu et déjà publié, compte GitHub protégé par
   l'**authentification à deux facteurs**, aucun logiciel malveillant ni contournement de sécurité,
   mention de SignPath sur la page du projet (déjà en place).

La décision appartient à SignPath ; le délai d'examen est de quelques jours à quelques semaines.

## 2. Une fois accepté : configurer SignPath

Dans l'interface SignPath :

1. **Projet** : identifiant (slug) `torrentuploader`, lié au dépôt GitHub.
2. **Configuration d'artefact** : slug `initial`, contenu = `.signpath/artifact-configuration.xml`.
3. **Politique de signature** : slug `release-signing` (certificat de la fondation), avec
   **approbation manuelle** par vous.
4. **Trusted build system** : GitHub.com, rattaché au projet.
5. Un **utilisateur CI** et son **jeton d'API**.

Si SignPath impose d'autres identifiants, modifiez `SIGNPATH_PROJECT` / `SIGNPATH_POLICY` en tête du
fichier `release.yml`.

## 3. Brancher GitHub

Dépôt → **Settings → Secrets and variables → Actions** :

- onglet **Secrets** : `SIGNPATH_API_TOKEN` = le jeton de l'utilisateur CI ;
- onglet **Variables** : `SIGNPATH_ORGANIZATION_ID` = l'identifiant de votre organisation SignPath.

## 4. Publier une version signée

1. Augmentez `__version__` dans `tuploader/__init__.py`, commitez.
2. Créez et poussez une étiquette : `git tag v2.0.1` puis `git push origin v2.0.1`.
3. GitHub Actions teste, construit, envoie à SignPath ; **approuvez la demande** dans SignPath
   (ou par le courriel reçu).
4. La publication GitHub est créée avec l'exécutable signé.

Le bouton **Run workflow** (onglet Actions) lance la même chaîne sans publier : pratique pour
vérifier la signature.

> Même signé, un nouveau certificat met un peu de temps à acquérir sa réputation auprès de
> SmartScreen : l'avertissement disparaît au fil des téléchargements, et cette réputation est ensuite
> conservée d'une version à l'autre.
