# Torrent Uploader — Chrome and Edge extension

*[Version française](README.fr.md)*

A Manifest V3 extension that forwards the torrents you meet in the browser to the
**Torrent Uploader** desktop application, which sends them to a ruTorrent
seedbox:

- **automatic capture**: as soon as a `.torrent` download finishes, its location is passed to the
  application — wherever the file was saved, with no second download;
- **right-click** on a magnet or `.torrent` link → "Envoyer vers la seedbox (Torrent Uploader)". The
  file is read from the page, so with your logged-in session on the site (private trackers).

It needs no configuration: it reaches the application through **native messaging** and **starts it
if it is closed**.

## Installation

1. In the application: **Intégration → Activer** (declares the native host to the browser).
2. `chrome://extensions` → **Developer mode** → **Load unpacked** → this folder.
   For Edge: `edge://extensions` → "Developer mode" → "Load unpacked".
3. Pin the icon: a green dot confirms the link works.

The folder must stay where it is: the browser reads it permanently.

## Files

| File | Role |
|---|---|
| `manifest.json` | MV3 manifest; the `key` field pins the ID `ejbkdabahogcncpmabcgmenfmefokgjj` |
| `sw.js` | service worker: capture, context menus, transports |
| `popup.html/js` | icon popup: status and current settings |
| `options.html/js` | options, diagnostics, manual connection |
| `ui.css`, `icons/` | look and feel (light or dark, following the system) |

The matching private key (`extension-key.pem`) is **never** distributed or committed: it is only
needed to pack the extension under the same ID.

## Permissions

| Permission | Purpose |
|---|---|
| `nativeMessaging` | reach the application (and start it) with no token or port |
| `downloads` | detect a finished `.torrent` download, delete it if requested |
| `contextMenus` | the "Envoyer vers la seedbox" entry |
| `scripting`, `activeTab` | read a `.torrent` from the clicked page, with the site's session |
| `storage`, `notifications` | settings, visual feedback |
| `host_permissions` `127.0.0.1`, `localhost` | manual fallback connection to the local HTTP service |

## Protocol

### Native messaging — host `com.torrentuploader.host`

| Message | Reply |
|---|---|
| `{"action": "ping"}` | `{"ok": true, "app": "Torrent Uploader", "version": "2.0.0", "running": true}` |
| `{"action": "show"}` | opens the application or brings it to the front |
| `{"action": "add", "items": [...]}` | `{"ok": true, "accepted": 1, "errors": [], "launched": false}` |

`launched` is `true` when the application was closed and has just been started.

### Items accepted in `items`

| Form | Example |
|---|---|
| magnet link | `"magnet:?xt=urn:btih:<40 hex or 32 base32>&dn=..."` |
| address | `"https://site/x.torrent"` (the seedbox will download it) |
| local file | `{"path": "C:\\...\\x.torrent", "consume": false}` |
| content | `{"name": "x.torrent", "data_b64": "<base64>"}` |

`consume: true` asks the application to make its own copy, so the original can be deleted.

### Fallback: local HTTP service

When native messaging is unavailable and a token is set in the options:
`POST http://127.0.0.1:38471/{ping|show|add}` with `{"token": "...", "items": [...]}`. The
application must then be open. Codes: `400` invalid body, `403` token refused or non-local `Host`,
`404` unknown route.
