<p align="center"><img src="assets/logo.png" width="160" alt="Logo : tête de pirate"></p>

<h1 align="center">Médiathèque NAS pilotée par Discord</h1>

<p align="center">
Films, séries, animes et mangas demandés depuis Discord, téléchargés et rangés tout seuls sur ton NAS.<br>
Une seule installation Docker : seuls 3 secrets et 3 dossiers sont à renseigner, tout le reste se configure automatiquement.
</p>

---

## Ce que ça fait

| Commande Discord | Effet |
|---|---|
| `/film titre:Dune` | Choisis le film dans la liste : il est cherché, téléchargé puis rangé dans `Films`. |
| `/serie titre:The Bear` | Pareil pour une série ; les nouveaux épisodes arrivent tout seuls à leur sortie. |
| `/manga titre:Solo Leveling` | Manga, manhwa ou webtoon : VF si une source française l'a, sinon en anglais. Chaque nouveau chapitre est récupéré automatiquement. |
| `/qb` | Torrents de qBittorrent avec onglets **En cours / Terminés / Tous** (progression, vitesse, temps restant, seeders, ratio). Menu pour annuler un téléchargement ou retirer un torrent terminé (avec ou sans ses fichiers), et bouton pour retirer d'un coup tous les terminés déjà rangés. |
| `/suivre titre:…` · `/suivis` | Être prévenu en message privé quand un film, un épisode ou un chapitre est prêt. |
| `/notifications` | Annoncer tout ce qui arrive dans le salon courant (réservé aux gérants du serveur). |
| `/etat` | Vérifie que chaque maillon fonctionne et explique ce qui bloque. |
| `/help` | Toutes les commandes. |

**Automatismes**

- **Choix des torrents** : VO privilégiée, versions uniquement doublées en français rejetées, minimum de seeders, qualité 1080p.
- **Animes** reconnus tout seuls et rangés dans `Anims` : VF > VOSTFR > multi-sous-titres > VOSTEN, avec remplacement automatique quand une meilleure version sort. Deux trackers d'anime publics (nekoBT, Nyaa.si) sont ajoutés d'office.
- **Recherche quotidienne** des films et épisodes manquants.
- **Notifications** : la personne qui fait une demande reçoit un message privé quand c'est prêt ; les autres cliquent sur **🔔 Me notifier**.

## Ce qui est installé

| Service | Rôle | Port |
|---|---|---|
| `arr-bot` | Bot Discord + configuration automatique de tout le reste | — |
| `radarr` / `sonarr` | Recherche, téléchargement et rangement des films / séries | 7878 / 8989 |
| `jackett` | Interroge les trackers (IPTorrents, nekoBT, Nyaa.si) | 9117 |
| `qbittorrent` | Client torrent | 8080 |
| `suwayomi` | Mangas / manhwas chapitre par chapitre (sources FR puis EN) | 4567 |
| `init` | Prépare les dossiers et qBittorrent au démarrage, puis s'arrête | — |

## Installation

Testé sur NAS UGREEN (UGOS Pro, application Docker). Fonctionne sur toute machine avec Docker Compose.

### 1. Créer le bot Discord

1. [Portail développeur Discord](https://discord.com/developers/applications) → **New Application**.
2. Onglet **Bot** → **Reset Token** → copie le token. Laisse les trois *Privileged Gateway Intents* désactivés.
3. Onglet **OAuth2 → URL Generator** : scopes `bot` + `applications.commands`, permissions **View Channels**, **Send Messages**, **Embed Links**. Ouvre l'URL générée et invite le bot sur ton serveur.
4. Optionnel : mets `assets/logo.png` comme avatar du bot.

### 2. Préparer IPTorrents

IPTorrents a un captcha : Jackett réutilise la session de ton navigateur.

1. Sur [iptorrents.com/settings.php](https://iptorrents.com/settings.php), règle *Torrents per page* = **100**, coche *Torrents - Show files count*, *Torrents - Category column* = **Icons**.
2. Toujours connecté, ouvre les outils de développement (**F12**) → onglet **Réseau** → **F5** → première ligne → **En-têtes de requête**.
3. Copie la valeur de `cookie` (`uid=…; pass=…`) et celle de `user-agent`.
4. Ne clique jamais sur *Logout* : ça invaliderait le cookie.

### 3. Récupérer le projet et remplir le `.env`

```bash
cd /volume1/docker
git clone https://github.com/<ton-compte>/<ce-depot>.git mediatheque
cd mediatheque
cp .env.example .env
```

Pas de SSH ? Télécharge le ZIP du dépôt depuis GitHub, décompresse-le dans le dossier partagé `docker` avec l'application Fichiers, puis crée le `.env` à partir de `.env.example`.

Ouvre `.env` et remplis-le :

| Variable | Contenu |
|---|---|
| `DISCORD_TOKEN` | Token du bot (étape 1) |
| `IPT_COOKIE` / `IPT_USERAGENT` | Cookie et User-Agent IPTorrents (étape 2) |
| `CONFIG_DIR` | Dossier des réglages des applis, ex. `/volume1/docker` |
| `MEDIA_DIR` | Dossier partagé des médias : `Films`, `Séries`, `Anims`, `Téléchargements` y sont créés |
| `MANGA_DIR` | Dossier des mangas, ex. `/volume1/Comics` |
| `PUID` / `PGID` | Compte propriétaire des fichiers (commande `id` en SSH) |

Les autres variables sont optionnelles et documentées dans `.env.example`.

### 4. Déployer

- **UGOS Pro** : application **Docker** → **Projet** → **Créer**, choisis le dossier du projet (il contient `docker-compose.yml` et `.env`) → **Déployer**.
- **Ligne de commande** : `docker compose up -d` dans le dossier du projet.

Attends 5 minutes, puis tape `/etat` sur Discord : tout doit être vert.

## Où arrivent les fichiers

```
MEDIA_DIR/
├── Films/              films
├── Séries/             séries
├── Anims/              animes (films et séries)
└── Téléchargements/    torrents en cours (même disque : rangement instantané, sans copie)
MANGA_DIR/
└── <Source>/<Titre>/<Chapitre>.cbz
```

Tous les fichiers sont nommés et accompagnés de métadonnées, ce qui les rend lisibles par Plex, Jellyfin, Komga, Kavita, etc.

## Mettre à jour

**Le code du bot se met à jour tout seul.** À chaque démarrage, les conteneurs `arr-bot` et `init` récupèrent la dernière version de la branche `main` sur GitHub (dans `CONFIG_DIR/arr-bot-code`) avant de se lancer. Pour mettre à jour : redémarre le conteneur `arr-bot` (ou le projet entier). Le journal affiche une ligne `[maj]` : `code mis à jour vers …` ou `déjà à jour`.

- Sans Internet, ou si la nouvelle version est incomplète ou ne se compile pas, la version déjà installée est gardée : le bot démarre quand même.
- `MAJ_AUTO=non` dans `.env` désactive la mise à jour ; `CODE_BRANCH` permet de suivre une autre branche.
- Ce qui ne se met **pas** à jour tout seul : `docker-compose.yml` et les images (Radarr, Sonarr…). Pour eux :

```bash
cd /volume1/docker/mediatheque
git pull
docker compose pull
docker compose up -d
```

Sur UGOS sans SSH : remplace les fichiers du dossier par ceux du nouveau ZIP (en gardant ton `.env`), puis **Projet → Redéployer**.

## Sécurité

- **Aucun secret n'est stocké dans le dépôt.** Le token Discord et le cookie IPTorrents vivent uniquement dans `.env`, ignoré par Git (`.gitignore`). Vérifie avec `git status` avant chaque `git push` : `.env` ne doit jamais apparaître.
- **Token publié par erreur ?** Régénère-le immédiatement (**Reset Token**), même si tu as supprimé le commit : l'historique Git reste lisible.
- Les interfaces web (Radarr, Sonarr, Jackett, qBittorrent, Suwayomi) n'ont pas de mot de passe au départ. Garde-les sur ton réseau local et **n'ouvre jamais leurs ports sur ta box**. Pour un accès à distance, passe par un VPN (Tailscale, WireGuard…).
- Le mot de passe de qBittorrent est généré au premier démarrage et écrit dans `CONFIG_DIR/qbittorrent/MOT_DE_PASSE_QBITTORRENT.txt`.

## Dépannage

| Symptôme | Solution |
|---|---|
| `CONFIG_DIR manquant dans .env` au déploiement | Le fichier `.env` n'est pas à côté de `docker-compose.yml`, ou porte un autre nom (`.env.txt`, `_env`…). |
| Le bot reste hors ligne | Journal du conteneur `arr-bot` : vérifie `DISCORD_TOKEN`. |
| `/etat` : IPTorrents refusé | Cookie expiré (déconnexion, changement de mot de passe) : recopie-le dans `.env` et redéploie. Connecte-toi au site au moins tous les 89 jours. |
| `/etat` : rangement bloqué | Droits d'écriture sur `MEDIA_DIR` pour le compte `PUID`. |
| Journal `[maj] mise à jour impossible` | GitHub injoignable depuis le NAS : le bot tourne sur la version déjà installée, rien à faire. |
| `/etat` : chapitres de mangas en erreur | Droits d'écriture sur `MANGA_DIR`, ou source en panne (les sites de scans changent souvent d'adresse). |
| Port déjà utilisé | Change le numéro de gauche du port concerné dans `docker-compose.yml`. |

## Avertissement

Ce projet automatise des outils libres existants. Utilise-le uniquement pour des contenus que tu as le droit de télécharger. Tu es seul responsable de l'usage que tu en fais.

## Crédits

[Radarr](https://radarr.video) · [Sonarr](https://sonarr.tv) · [Jackett](https://github.com/Jackett/Jackett) · [qBittorrent](https://www.qbittorrent.org) · [Suwayomi](https://github.com/Suwayomi/Suwayomi-Server) · [discord.py](https://github.com/Rapptz/discord.py) · images [LinuxServer.io](https://www.linuxserver.io)
