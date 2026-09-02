# Torrent Uploader

Application Windows (Python + tkinter) qui envoie des fichiers `.torrent` et des liens
**magnet** vers une seedbox **ruTorrent / rTorrent**, en choisissant le **dossier de
destination** et l'**étiquette (label)**.

Depuis la version 2, elle s'intègre à Chrome : un clic sur un torrent dans le navigateur
peut atterrir directement dans la file d'attente.

---

## 1. Installation

Rien à installer : le script n'utilise que la bibliothèque standard de Python 3.

Option recommandée — glisser-déposer **dans la fenêtre** :

```bash
pip install tkinterdnd2
```

Sans ce paquet, tout fonctionne : dépôt sur le raccourci, bouton « Ajouter… », et toutes
les voies d'interception décrites plus bas.

## 2. Premier lancement

Double-cliquez sur **`Torrent Uploader.bat`**. La fenêtre des paramètres s'ouvre
automatiquement :

| Champ | Exemple | Remarque |
|---|---|---|
| URL de ruTorrent | `https://maseedbox.com/rutorrent` | racine de ruTorrent, sans `/php/addtorrent.php` |
| Identifiant / Mot de passe | ceux de l'interface web | authentification HTTP Basic ou Digest |
| Vérifier le certificat SSL | coché | à décocher seulement si certificat auto-signé |

**« Tester la connexion »** lit la liste des torrents existants et affiche le nombre
d'étiquettes et de dossiers détectés. Puis **Enregistrer**.

Réglages stockés dans `config.json`, à côté du script.
⚠️ Le mot de passe y est en clair — ne partagez pas ce fichier.

## 3. Utilisation

1. Amenez un ou plusieurs torrents dans la file (voir §4 pour les manières de le faire).
2. Choisissez le **dossier de destination** et l'**étiquette**. Vide = valeurs par défaut
   de rTorrent.
3. Cochez ou non « Démarrer le torrent immédiatement ».
4. **Envoyer vers la seedbox**.

La colonne **Statut** affiche le résultat par torrent. « Ajouté (confirmé) » signifie que
l'application a réinterrogé la seedbox et y a retrouvé l'infohash : l'ajout est réellement
effectif. « Envoyé (non confirmé) » signifie que la seedbox a accepté la requête mais que
la vérification n'a pas pu aboutir (plugin `httprpc` absent, seedbox lente, ou lien sans
infohash).

La colonne **Type** distingue :

| Type | Origine | Envoi |
|---|---|---|
| Fichier | un `.torrent` sur le disque | le fichier est téléversé |
| Aimant | un lien `magnet:` | le lien est transmis à ruTorrent |
| Lien | une URL `http(s)` vers un `.torrent` | ruTorrent le télécharge lui-même |

### Détails pratiques

- Boutons **`+` / `-`** : ajoutent ou retirent la valeur courante de vos favoris.
- **« Actualiser »** relit les étiquettes et dossiers réellement utilisés sur la seedbox.
- Vous pouvez déposer plusieurs torrents, ou un **dossier** entier.
- Le dernier dossier et la dernière étiquette utilisés sont pré-remplis au lancement suivant.
- Touche `Suppr` : retire les lignes sélectionnées.
- Un même torrent n'entre qu'une fois dans la liste, même s'il arrive une fois comme
  fichier et une fois comme magnet : le dédoublonnage se fait sur l'infohash.

---

## 4. Interception depuis Chrome

Trois mécanismes indépendants, cumulables. Tout se règle via le bouton
**« Intégration Chrome… »** de la fenêtre principale.

Quelle que soit la voie utilisée, **une seule fenêtre** de l'application est ouverte :
un second lancement transmet ses torrents à la fenêtre existante puis se termine.

### 4.1 Association Windows — la voie principale

Bouton **« Activer »** dans la section 1 (ou `Activer l'integration Windows.bat`).

L'application se déclare gestionnaire des fichiers `.torrent` et des liens `magnet:`.
Ensuite, dans Chrome :

- un clic sur un `.torrent` téléchargé l'ouvre ici ;
- **« Toujours ouvrir ce type de fichier »** dans la barre de téléchargement rend
  l'opération automatique pour les suivants ;
- un clic sur un lien `magnet:` ouvre l'application (Chrome demande une confirmation la
  première fois ; cochez « Toujours autoriser » pour ne plus la revoir).

Tout est écrit dans `HKEY_CURRENT_USER` : **aucun droit administrateur**, et
**« Désactiver »** retire exactement ce qui a été écrit.

Si aucun client torrent n'est installé, `.torrent` et `magnet:` ne sont revendiqués par
personne et tombent sur la boîte « Ouvrir avec » de Windows : l'activation prend alors
effet immédiatement. Le bouton affiche l'état réel après l'opération.

> Si un client torrent est installé plus tard, il deviendra prioritaire. L'application ne
> l'écrase jamais : elle s'ajoute simplement au menu **« Ouvrir avec »**. Pour reprendre la
> main, passez par le bouton « Applications par défaut de Windows » — Windows exige un clic
> de l'utilisateur, aucun programme ne peut s'imposer comme défaut.

### 4.2 Surveillance du dossier de téléchargement

Section 2. **Désactivée par défaut.**

L'application surveille votre dossier `Téléchargements` et importe automatiquement tout
nouveau `.torrent` qui y apparaît. Utile si Chrome enregistre les torrents sans les ouvrir.

- Les fichiers **déjà présents au démarrage sont ignorés** : seules les nouveautés comptent.
- Les `.crdownload` en cours d'écriture sont ignorés ; le fichier n'est pris en compte
  qu'une fois réellement complet et décodable.
- Une page d'erreur HTML enregistrée sous le nom `.torrent` est rejetée.
- Option **« Envoyer immédiatement »** : le torrent part sans aucun clic, avec le dossier
  et l'étiquette affichés dans la fenêtre.

> Si Chrome est réglé sur « demander où enregistrer chaque fichier », les torrents peuvent
> atterrir ailleurs que dans le dossier surveillé. Dans ce cas, préférez l'association
> Windows (§4.1) ou l'extension (§4.3).

### 4.3 Extension Chrome — clic droit sur un lien

Section 3. C'est la seule voie qui agit **avant** le téléchargement.

**Installation :**

1. Chrome → `chrome://extensions` → activez **Mode développeur** (en haut à droite).
2. **« Charger l'extension non empaquetée »** → choisissez le dossier `chrome-extension`
   (le bouton « Ouvrir le dossier de l'extension » vous y emmène).
3. Dans l'application : section 3 → **Copier** le jeton.
4. Sur la fiche de l'extension → **Détails** → **Options** → collez le jeton →
   **Enregistrer** → **Tester la connexion**.

**Usage :** clic droit sur un lien `.torrent` ou `magnet` → « Envoyer vers la seedbox ».
Fonctionne aussi sur du texte sélectionné (un magnet copié dans une page).

**Capture automatique** (option, désactivée par défaut) : intercepte les téléchargements
de `.torrent` sans jamais les écrire sur le disque. Elle nécessite d'accorder à l'extension
l'accès aux sites visités, afin de lire le fichier **avec vos cookies de connexion** —
indispensable sur un tracker privé, où la seedbox seule ne recevrait qu'une page d'erreur.

> ⚠️ Cette option redemande le fichier au site. Sur un tracker qui n'autorise qu'un seul
> téléchargement par jeton, utilisez plutôt le clic droit ou la surveillance de dossier.

**Sécurité :** l'application écoute sur `127.0.0.1:38471` et **refuse toute requête sans le
jeton**, qui est propre à votre installation. Une page web ne peut pas le deviner, et la
réponse du service ne lui est de toute façon jamais lisible.

---

## 5. Fonctionnement technique

- **Fichiers** : `POST /php/addtorrent.php` en `multipart/form-data`
  (`torrent_file`, `dir_edit`, `label`, `torrents_start_stopped`).
- **Magnets et URL** : même page, en `x-www-form-urlencoded`, champ `url`.
- **Étiquettes et dossiers** : `POST /plugins/httprpc/action.php` (`mode=list`),
  champ 14 = label, champ 25 = chemin.
- **Confirmation** : l'infohash est calculé localement (SHA-1 de la section `info`, ou
  extrait du magnet en convertissant le base32 en hexadécimal) puis recherché sur la seedbox.
- **Instance unique** : verrou par `SO_EXCLUSIVEADDRUSE` sur `127.0.0.1:38471`. Le second
  processus transmet ses arguments au premier et se termine.
- **Threads** : aucun thread ne touche à tkinter. Ils déposent des tâches dans une file que
  le thread principal draine — sans quoi Python lève `main thread is not in main loop`.
- **Journal** : sous `pythonw.exe` il n'existe aucune console ; toute erreur inattendue est
  écrite dans `erreurs.log`, à côté du script.

## 6. Problèmes fréquents

| Message | Cause probable |
|---|---|
| `Authentification refusée (HTTP 401)` | identifiant ou mot de passe incorrect |
| `Page introuvable (HTTP 404)` | l'URL ne pointe pas sur la racine de ruTorrent |
| `Erreur SSL` | certificat auto-signé → décochez « Vérifier le certificat SSL » |
| `le plugin httprpc est-il activé ?` | l'envoi fonctionne, mais étiquettes, dossiers et confirmation sont indisponibles |
| `Envoyé (non confirmé)` | parti, mais non revérifiable (httprpc absent, seedbox lente, ou lien sans infohash) |
| L'extension dit « Torrent Uploader injoignable » | l'application n'est pas ouverte, ou le port diffère |
| L'extension dit « Jeton refusé » | recopiez le jeton depuis la section 3 |
| `Service local INACTIF (port occupé)` | un autre programme occupe le port 38471 |

Si ruTorrent refuse le dossier de destination, c'est en général que le chemin sort de la
directive `$topDirectory` de sa configuration : utilisez un chemin sous votre dossier de
téléchargement habituel.

## 7. Fichiers

| Fichier | Rôle |
|---|---|
| `torrent_uploader.py` | l'application |
| `windows_integration.py` | associations Windows (registre HKCU) |
| `chrome-extension/` | extension Chrome MV3 |
| `Torrent Uploader.bat` | lanceur, accepte les fichiers déposés dessus |
| `Activer l'integration Windows.bat` | équivalent du bouton « Activer » |
| `Desactiver l'integration Windows.bat` | retire les associations |
| `config.json` | réglages (créé au premier enregistrement) |
| `ipc_token.txt` | jeton du service local (créé au premier lancement) |
| `erreurs.log` | journal des erreurs (créé en cas de problème) |

### Ligne de commande

```bash
python torrent_uploader.py --status
```

`--register` et `--unregister` font la même chose que les boutons correspondants.
