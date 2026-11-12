# Torrent Uploader — extension Chrome et Edge

*[English version](README.md)*

Extension (Manifest V3) qui fait parvenir à l'application **Torrent Uploader** les torrents
rencontrés dans le navigateur :

- **capture automatique** : dès qu'un téléchargement de `.torrent` se termine, son emplacement est
  transmis à l'application — où que le fichier ait été enregistré, et sans second téléchargement ;
- **clic droit** sur un lien magnet ou `.torrent` → « Envoyer vers la seedbox (Torrent Uploader) ».
  Le fichier est lu depuis la page, donc avec la session ouverte sur le site (trackers privés).

Elle ne demande aucun réglage : elle joint l'application par **messagerie native** et la **lance si
elle est fermée**.

## Installation

1. Dans l'application : **Intégration → Activer** (déclare l'hôte natif au navigateur).
2. `chrome://extensions` → **Mode développeur** → **Charger l'extension non empaquetée** → ce dossier.
   Pour Edge : `edge://extensions` → « Mode développeur » → « Charger l'extension décompressée ».
3. Épinglez l'icône : un point vert confirme la liaison.

Le dossier doit rester en place : le navigateur le lit en permanence.

## Fichiers

| Fichier | Rôle |
|---|---|
| `manifest.json` | déclaration MV3 ; la clé `key` fixe l'identifiant `ejbkdabahogcncpmabcgmenfmefokgjj` |
| `sw.js` | service worker : capture, menus contextuels, transports |
| `popup.html/js` | fenêtre de l'icône : état et réglages courants |
| `options.html/js` | options, diagnostic, connexion manuelle |
| `ui.css`, `icons/` | apparence (claire ou sombre selon le système) |

La clé privée correspondante (`extension-key.pem`) n'est **jamais** distribuée : elle ne sert qu'à
empaqueter l'extension avec le même identifiant.

## Permissions

| Permission | Usage |
|---|---|
| `nativeMessaging` | joindre l'application (et la lancer) sans jeton ni port |
| `downloads` | repérer la fin d'un téléchargement de `.torrent`, le supprimer si demandé |
| `contextMenus` | entrée « Envoyer vers la seedbox » |
| `scripting`, `activeTab` | lire un `.torrent` depuis la page cliquée, avec la session du site |
| `storage`, `notifications` | réglages, retour visuel |
| `host_permissions` `127.0.0.1`, `localhost` | connexion manuelle de secours au service HTTP local |

## Protocole

### Messagerie native — hôte `com.torrentuploader.host`

Message (JSON) → réponse :

| Message | Réponse |
|---|---|
| `{"action": "ping"}` | `{"ok": true, "app": "Torrent Uploader", "version": "2.0.0", "running": true}` |
| `{"action": "show"}` | ouvre l'application ou la met au premier plan |
| `{"action": "add", "items": [...]}` | `{"ok": true, "accepted": 1, "errors": [], "launched": false}` |

`launched` vaut `true` si l'application était fermée et vient d'être démarrée.

### Éléments acceptés dans `items`

| Forme | Exemple |
|---|---|
| lien magnet | `"magnet:?xt=urn:btih:<40 hex ou 32 base32>&dn=..."` |
| adresse | `"https://site/x.torrent"` (la seedbox le téléchargera) |
| fichier local | `{"path": "C:\\...\\x.torrent", "consume": false}` |
| contenu | `{"name": "x.torrent", "data_b64": "<base64>"}` |

`consume: true` demande à l'application d'en faire sa propre copie : l'original peut alors être
supprimé.

### Secours : service HTTP local

Si la messagerie native n'est pas disponible et qu'un jeton est saisi dans les options :
`POST http://127.0.0.1:38471/{ping|show|add}` avec `{"token": "...", "items": [...]}`.
L'application doit alors être ouverte. Codes : `400` corps invalide, `403` jeton refusé ou
`Host` non local, `404` route inconnue.
