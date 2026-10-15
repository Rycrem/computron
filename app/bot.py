"""
Bot Discord tout-en-un — rien à configurer à part DISCORD_TOKEN.
  /film <titre>   -> ajoute un film dans Radarr et lance la recherche
  /serie <titre>  -> ajoute une série dans Sonarr et lance la recherche
  /qb             -> torrents en cours et terminés (seeders inclus) + menu pour annuler / retirer
  /manga <titre>  -> manga / manhwa via Suwayomi (VF en priorité, sinon anglais)
  /mangas         -> avancement des téléchargements de mangas (aussi en onglet dans /qb)
  /etat           -> vérifie que tout est bien branché (Radarr, Sonarr, Jackett, qBittorrent)
  /notifications  -> choisit le salon où annoncer les films et épisodes prêts à regarder
  /suivre <titre> -> être prévenu en message privé (le demandeur l'est automatiquement)
  /suivis         -> liste de ses abonnements
  /help           -> toutes les commandes
Chaque nuit (SEARCH_HOUR), relance la recherche des films et épisodes manquants.
Au démarrage, le bot configure automatiquement toute la stack (voir autoconfig.py).
"""
import asyncio
import json
import os

import aiohttp
import discord
from discord import app_commands

import autoconfig as ac
import manga
from types import SimpleNamespace

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
ALLOWED_ROLE = os.getenv("DISCORD_ALLOWED_ROLE", "").strip()

http: aiohttp.ClientSession | None = None


# ---------- Radarr / Sonarr ----------
class Arr:
    def __init__(self, name, url, app, kind):
        self.name, self.url, self.app, self.kind = name, url, app, kind

    async def _req(self, method, path, **kw):
        key = ac.arr_key(self.app)
        if not key:
            raise RuntimeError(f"{self.name} n'a pas encore démarré, réessaie dans une minute.")
        async with http.request(method, f"{self.url}/api/v3/{path}",
                                headers={"X-Api-Key": key}, **kw) as r:
            if r.status >= 400:
                raise RuntimeError(f"{self.name} a répondu {r.status} : {(await r.text())[:300]}")
            txt = await r.text()
            return json.loads(txt) if txt.strip() else None

    async def lookup(self, term):
        return await self._req("GET", f"{self.kind}/lookup", params={"term": term})

    async def add(self, item):
        profiles = await self._req("GET", "qualityprofile")
        roots = await self._req("GET", "rootfolder")
        if not roots:
            raise RuntimeError(f"{self.name} n'est pas encore configuré. Tape /etat pour vérifier.")
        anime = is_anime(item)
        wanted_profile = ac.ANIME_PROFILE if anime else ac.PREFERRED_PROFILE
        profile = (next((p for p in profiles if p["name"] == wanted_profile), None)
                   or next((p for p in profiles if p["name"] == ac.PREFERRED_PROFILE), profiles[0]))
        item = dict(item)
        item.update({"qualityProfileId": profile["id"], "rootFolderPath": pick_root(roots, anime, self),
                     "monitored": True})
        if self.kind == "movie":
            item["minimumAvailability"] = "released"
            item["addOptions"] = {"searchForMovie": True}
        else:
            item["seasonFolder"] = True
            item["seriesType"] = "anime" if anime else "standard"
            item["addOptions"] = {"monitor": "all", "searchForMissingEpisodes": True}
        return await self._req("POST", self.kind, json=item)


ASIAN_ANIME_LANGS = {"japanese", "chinese", "korean", "mandarin", "cantonese"}


def is_anime(item):
    """Anime = genre « Anime », ou animation dont la langue d'origine est japonaise / chinoise / coréenne."""
    if item.get("seriesType") == "anime":
        return True
    genres = {str(g).lower() for g in item.get("genres") or []}
    lang = str((item.get("originalLanguage") or {}).get("name", "")).lower()
    return "anime" in genres or ("animation" in genres and lang in ASIAN_ANIME_LANGS)


def pick_root(roots, anime, arr):
    """Dossier Anims pour les animes, Films / Séries sinon."""
    def base(r):
        return ac.norm(r["path"].rstrip("/").rsplit("/", 1)[-1])
    wanted = ac.ANIME_DIR_ALIASES if anime else ({"films"} if arr.kind == "movie" else {"series"})
    match = next((r for r in roots if base(r) in wanted), None)
    if match:
        return match["path"]
    others = [r for r in roots if base(r) not in ac.ANIME_DIR_ALIASES] if not anime else roots
    return (others or roots)[0]["path"]


radarr = Arr("Radarr", ac.RADARR, "radarr", "movie")
sonarr = Arr("Sonarr", ac.SONARR, "sonarr", "series")


# ---------- qBittorrent ----------
async def qb_torrents():
    url = f"{ac.QBIT}/api/v2/torrents/info"
    params = {"sort": "added_on", "reverse": "true"}
    headers = {"Referer": ac.QBIT}
    for attempt in range(2):
        async with http.get(url, params=params, headers=headers) as r:
            if r.status == 200:
                return await r.json()
            if r.status != 403 or attempt:
                raise RuntimeError(f"qBittorrent a répondu {r.status}")
        # Normalement inutile (accès autorisé depuis Docker), mais on tente la connexion
        async with http.post(f"{ac.QBIT}/api/v2/auth/login", headers=headers,
                             data={"username": "admin", "password": ac.qbit_password()}) as r:
            await r.text()
    raise RuntimeError("Accès à qBittorrent refusé.")


STATES = {
    "downloading": "⬇️ En cours", "forcedDL": "⬇️ Forcé", "metaDL": "🔎 Métadonnées",
    "stalledDL": "⏳ Bloqué (pas de sources)", "queuedDL": "🕒 En file", "pausedDL": "⏸️ Pause",
    "stoppedDL": "⏸️ Arrêté", "checkingDL": "🔍 Vérification", "moving": "📦 Déplacement",
    "error": "❌ Erreur", "missingFiles": "❌ Fichiers manquants",
    "uploading": "🌱 En partage", "forcedUP": "🌱 Partage forcé", "stalledUP": "✅ Terminé",
    "queuedUP": "✅ Terminé", "pausedUP": "✅ Terminé (partage arrêté)",
    "stoppedUP": "✅ Terminé (partage arrêté)", "checkingUP": "🔍 Vérification",
}


def bar(p, n=14):
    full = round(p * n)
    return "█" * full + "░" * (n - full)


def size(b):
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} Po"


def eta(s):
    if s <= 0 or s >= 8640000:
        return "∞"
    h, m = divmod(s // 60, 60)
    d, h = divmod(h, 24)
    return f"{d}j {h}h" if d else (f"{h}h {m}min" if h else f"{m}min")


def seeds(t):
    """Seeders connectés / seeders disponibles dans l'essaim."""
    total = t.get("num_complete", -1)
    return f"🌱 {t.get('num_seeds', 0)}/{total if total >= 0 else '?'}"


QB_FILTERS = {"actifs": "⬇️ En cours", "termines": "✅ Terminés", "tous": "📋 Tous"}
QB_PAGE = 8


def qb_filter(torrents, filtre):
    if filtre == "actifs":
        return [t for t in torrents if t["progress"] < 1]
    if filtre == "termines":
        return [t for t in torrents if t["progress"] >= 1]
    return torrents


def qb_field(t):
    state = STATES.get(t["state"], t["state"])
    if t["progress"] >= 1:
        up = f" · ↑ {size(t['upspeed'])}/s" if t.get("upspeed") else ""
        return (f"{state} · {size(t['size'])} · ratio {t.get('ratio', 0):.2f}{up}"
                + (f" · fini <t:{t['completion_on']}:R>" if t.get("completion_on", 0) > 0 else ""))
    speed = f"{size(t['dlspeed'])}/s" if t["dlspeed"] else "—"
    return (f"`{bar(t['progress'])}` **{t['progress'] * 100:.1f}%**\n"
            f"{state} · {speed} · ETA {eta(t['eta'])} · {size(t['size'])} · {seeds(t)}")


async def qb_embed(filtre="actifs", page=0):
    """Renvoie (embed, torrents de la page, page corrigée, nombre de pages)."""
    torrents = await qb_torrents()
    shown = qb_filter(torrents, filtre)
    pages = max(1, -(-len(shown) // QB_PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = shown[page * QB_PAGE:(page + 1) * QB_PAGE]
    active = sum(1 for t in torrents if t["progress"] < 1)
    e = discord.Embed(title=f"📥 Téléchargements — {QB_FILTERS[filtre]}", color=0x2F80ED)
    if not shown:
        e.description = {"actifs": "Aucun téléchargement en cours. 🎉",
                         "termines": "Aucun torrent terminé dans qBittorrent."}.get(filtre, "qBittorrent est vide.")
    for t in chunk:
        e.add_field(name=t["name"][:250], value=qb_field(t), inline=False)
    e.set_footer(text=f"Page {page + 1}/{pages} · {active} en cours · {len(torrents) - active} terminés · "
                      f"↓ {size(sum(t['dlspeed'] for t in torrents))}/s · "
                      f"↑ {size(sum(t.get('upspeed', 0) for t in torrents))}/s · 🌱 seeders connectés/disponibles")
    return e, chunk, page, pages


async def qb_delete(hashes, delete_files: bool):
    async with http.post(f"{ac.QBIT}/api/v2/torrents/delete", headers={"Referer": ac.QBIT},
                         data={"hashes": "|".join(hashes), "deleteFiles": "true" if delete_files else "false"}) as r:
        if r.status != 200:
            raise RuntimeError(f"qBittorrent a répondu {r.status}")


async def refresh_qb_message(origin, filtre, page):
    """Remet à jour le tableau /qb (message caché) après une suppression, via l'interaction qui l'a affiché."""
    if not origin:
        return
    try:
        embed, chunk, page, pages = await qb_embed(filtre, page)
        await origin.edit_original_response(embed=embed, view=QbView(chunk, filtre, page, pages, origin))
    except Exception as ex:
        print(f"[discord] actualisation de /qb impossible : {ex}", flush=True)


# ---------- Gestion des interactions expirées ----------
def is_expired(error):
    err = getattr(error, "original", error)
    return isinstance(err, discord.NotFound) and getattr(err, "code", None) == 10062


class BaseView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        if is_expired(error):
            print("[discord] clic expiré (le bot a répondu après 3 s) — ignoré", flush=True)
            return
        await super().on_error(interaction, error, item)


async def announce(interaction, view, content=None, embed=None):
    """Seul message public du bot : l'annonce avec le bouton « Me notifier » pour que les autres s'abonnent.
    Le message caché du demandeur est simplement refermé."""
    await interaction.edit_original_response(content="✅ C'est noté, je l'annonce dans le salon.", embed=None, view=None)
    kw = {"view": view} if view else {}
    try:  # message normal dans le salon (pas de « réponse à un message caché »)
        await interaction.channel.send(content=content, embed=embed,
                                       allowed_mentions=discord.AllowedMentions.none(), **kw)
    except (discord.Forbidden, AttributeError):  # pas le droit d'écrire ici : réponse publique à l'interaction
        await interaction.followup.send(content=content, embed=embed,
                                        allowed_mentions=discord.AllowedMentions.none(), **kw)


# ---------- Annulation ----------
async def queued_hashes():
    """Hashes des torrents que Radarr ou Sonarr n'ont pas encore rangés dans la médiathèque."""
    hashes = set()
    for arr in (radarr, sonarr):
        q = await arr._req("GET", "queue", params={"pageSize": 1000})
        hashes |= {(r.get("downloadId") or "").upper() for r in q.get("records", [])}
    return hashes


async def find_in_queue(torrent_hash):
    """Retrouve le téléchargement dans la file de Radarr ou Sonarr (downloadId = hash du torrent)."""
    h = torrent_hash.upper()
    for arr, extra in ((radarr, {"includeMovie": "true"}),
                       (sonarr, {"includeSeries": "true", "includeEpisode": "true"})):
        try:
            q = await arr._req("GET", "queue", params={"pageSize": 500, **extra})
        except Exception:
            continue
        records = [r for r in q.get("records", []) if (r.get("downloadId") or "").upper() == h]
        if records:
            return arr, records
    return None, []


async def cancel_download(torrent_hash, retry: bool):
    """retry=True  : écarte ce torrent (liste noire) et cherche une autre version.
       retry=False : supprime le téléchargement et retire le titre de Radarr/Sonarr."""
    arr, records = await find_in_queue(torrent_hash)
    if not arr:
        async with http.post(f"{ac.QBIT}/api/v2/torrents/delete", headers={"Referer": ac.QBIT},
                             data={"hashes": torrent_hash, "deleteFiles": "true"}) as r:
            if r.status != 200:
                raise RuntimeError(f"qBittorrent a répondu {r.status}")
        return "Torrent supprimé de qBittorrent (il n'était suivi ni par Radarr ni par Sonarr)."

    ids = [r["id"] for r in records]
    params = {"removeFromClient": "true", "blocklist": "true" if retry else "false",
              "skipRedownload": "false" if retry else "true"}
    await arr._req("DELETE", "queue/bulk", params=params, json={"ids": ids})
    if retry:
        return f"Version écartée : {arr.name} cherche un autre torrent automatiquement."

    if arr is radarr:
        movie_id = records[0].get("movieId")
        if movie_id:
            await radarr._req("DELETE", f"movie/{movie_id}",
                              params={"deleteFiles": "true", "addImportExclusion": "false"})
            drop_subscriptions(f"radarr:{movie_id}")
        return "Téléchargement annulé et film retiré de Radarr. Tu peux le redemander avec /film."

    series_id = records[0].get("seriesId")
    episode_ids = [r["episodeId"] for r in records if r.get("episodeId")]
    series = await sonarr._req("GET", f"series/{series_id}") if series_id else None
    if series and (series.get("statistics") or {}).get("episodeFileCount", 0) == 0:
        await sonarr._req("DELETE", f"series/{series_id}",
                          params={"deleteFiles": "true", "addImportListExclusion": "false"})
        drop_subscriptions(f"sonarr:{series_id}")
        return "Téléchargement annulé et série retirée de Sonarr. Tu peux la redemander avec /serie."
    if episode_ids:
        await sonarr._req("PUT", "episode/monitor", json={"episodeIds": episode_ids, "monitored": False})
    return ("Téléchargement annulé. La série garde ses épisodes déjà téléchargés ; "
            "ceux-ci ne seront plus recherchés (redemande la série avec /serie pour les relancer).")


async def relaunch(arr: "Arr", item):
    """Titre déjà dans Radarr/Sonarr : le réactive et relance la recherche s'il manque des fichiers."""
    if arr is radarr:
        movie = await radarr._req("GET", f"movie/{item['id']}")
        if movie.get("hasFile"):
            return None
        movie["monitored"] = True
        await radarr._req("PUT", f"movie/{movie['id']}", json=movie)
        await radarr._req("POST", "command", json={"name": "MoviesSearch", "movieIds": [movie["id"]]})
        return movie
    series = await sonarr._req("GET", f"series/{item['id']}")
    if (series.get("statistics") or {}).get("percentOfEpisodes", 0) >= 100:
        return None
    series["monitored"] = True
    await sonarr._req("PUT", f"series/{series['id']}", json=series)
    episodes = await sonarr._req("GET", "episode", params={"seriesId": series["id"]})
    ids = [e["id"] for e in episodes if not e.get("hasFile")]
    if ids:
        await sonarr._req("PUT", "episode/monitor", json={"episodeIds": ids, "monitored": True})
    await sonarr._req("POST", "command", json={"name": "SeriesSearch", "seriesId": series["id"]})
    return series


class ConfirmCancelView(BaseView):
    """Torrent pas encore terminé."""
    def __init__(self, torrent_hash, name, parent=None):
        super().__init__(timeout=120)
        self.hash, self.name, self.parent = torrent_hash, name, parent

    async def _do(self, interaction, retry):
        await interaction.response.edit_message(content=f"⏳ Traitement de **{self.name[:80]}**…", view=None)
        try:
            msg = await cancel_download(self.hash, retry)
            await interaction.edit_original_response(content=f"✅ **{self.name[:80]}**\n{msg}")
        except Exception as ex:
            await interaction.edit_original_response(content=f"❌ {ex}")
        if self.parent:
            await refresh_qb_message(*self.parent)

    @discord.ui.button(label="Annuler définitivement", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, _):
        await self._do(interaction, retry=False)

    @discord.ui.button(label="Chercher une autre version", emoji="🔁", style=discord.ButtonStyle.primary)
    async def retry(self, interaction, _):
        await self._do(interaction, retry=True)

    @discord.ui.button(label="Retour", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, _):
        await interaction.response.edit_message(content="Rien n'a été modifié.", view=None)


class ConfirmRemoveView(BaseView):
    """Torrent terminé : le retirer de qBittorrent, avec ou sans ses fichiers téléchargés."""
    def __init__(self, hashes, label, parent=None):
        super().__init__(timeout=120)
        self.hashes, self.label, self.parent = hashes, label, parent

    async def _do(self, interaction, delete_files):
        await interaction.response.edit_message(content=f"⏳ Retrait de {self.label}…", view=None)
        try:
            await qb_delete(self.hashes, delete_files)
            detail = ("fichiers du dossier Téléchargements supprimés, la copie rangée dans la médiathèque est conservée"
                      if delete_files else "fichiers conservés sur le disque")
            await interaction.edit_original_response(content=f"✅ {self.label} retiré de qBittorrent ({detail}).")
        except Exception as ex:
            await interaction.edit_original_response(content=f"❌ {ex}")
        if self.parent:
            await refresh_qb_message(*self.parent)

    @discord.ui.button(label="Retirer (garder les fichiers)", emoji="➖", style=discord.ButtonStyle.primary)
    async def keep(self, interaction, _):
        await self._do(interaction, delete_files=False)

    @discord.ui.button(label="Retirer et supprimer les fichiers", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def wipe(self, interaction, _):
        await self._do(interaction, delete_files=True)

    @discord.ui.button(label="Retour", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, _):
        await interaction.response.edit_message(content="Rien n'a été modifié.", view=None)


class QbView(BaseView):
    def __init__(self, chunk, filtre="actifs", page=0, pages=1, origin=None):
        super().__init__(timeout=840)  # un message caché ne peut plus être modifié après 15 min
        self.filtre, self.page, self.pages, self.origin = filtre, page, pages, origin
        self.torrents = {t["hash"]: t for t in chunk}

        for key, label in QB_FILTERS.items():
            btn = discord.ui.Button(label=label, row=0, disabled=key == filtre,
                                    style=discord.ButtonStyle.primary if key == filtre else discord.ButtonStyle.secondary)
            btn.callback = self._goto(key, 0)
            self.add_item(btn)
        self.add_item(mangas_tab_button())

        prev = discord.ui.Button(emoji="◀️", row=1, disabled=page == 0, style=discord.ButtonStyle.secondary)
        prev.callback = self._goto(filtre, page - 1)
        nxt = discord.ui.Button(emoji="▶️", row=1, disabled=page >= pages - 1, style=discord.ButtonStyle.secondary)
        nxt.callback = self._goto(filtre, page + 1)
        refresh = discord.ui.Button(label="Actualiser", emoji="🔄", row=1, style=discord.ButtonStyle.secondary)
        refresh.callback = self._goto(filtre, page)
        for b in (prev, nxt, refresh):
            self.add_item(b)
        if filtre != "actifs":
            clean = discord.ui.Button(label="Retirer tous les terminés", emoji="🧹", row=1,
                                      style=discord.ButtonStyle.danger)
            clean.callback = self.on_clean
            self.add_item(clean)

        if chunk:
            select = discord.ui.Select(
                placeholder="Retirer ou annuler un torrent…", row=2,
                options=[discord.SelectOption(
                    label=t["name"][:100], value=t["hash"], emoji="✅" if t["progress"] >= 1 else "⬇️",
                    description=(f"Terminé · {size(t['size'])} · ratio {t.get('ratio', 0):.2f}" if t["progress"] >= 1
                                 else f"{t['progress'] * 100:.0f}% · {size(t['size'])} · {seeds(t)}")[:100])
                    for t in chunk])
            select.callback = self.on_select
            self.add_item(select)

    def _goto(self, filtre, page):
        async def callback(interaction: discord.Interaction):
            if not allowed(interaction):
                return await interaction.response.send_message("⛔ Tu n'as pas le rôle requis.", ephemeral=True)
            await interaction.response.defer()  # répondre à Discord avant le délai de 3 s
            try:
                embed, chunk, p, pages = await qb_embed(filtre, page)
                await interaction.edit_original_response(embed=embed, view=QbView(chunk, filtre, p, pages, interaction))
            except Exception as ex:
                await interaction.followup.send(f"❌ {ex}", ephemeral=True)
        return callback

    async def on_select(self, interaction: discord.Interaction):
        if not allowed(interaction):
            return await interaction.response.send_message("⛔ Tu n'as pas le rôle requis.", ephemeral=True)
        h = interaction.data["values"][0]
        t = self.torrents.get(h, {"name": "ce téléchargement", "progress": 0})
        name, parent = t["name"], (self.origin, self.filtre, self.page)
        if t["progress"] < 1:
            return await interaction.response.send_message(
                f"Que faire de **{name[:150]}** ?\n"
                f"• **Annuler définitivement** : supprime le téléchargement et retire le titre de Radarr/Sonarr (tu pourras le redemander).\n"
                f"• **Chercher une autre version** : écarte ce torrent et en prend un autre.",
                view=ConfirmCancelView(h, name, parent), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        arr, _ = await find_in_queue(h)  # ignore Radarr/Sonarr injoignables : pas d'avertissement
        warn = (f"\n⚠️ {arr.name} ne l'a pas encore rangé dans la médiathèque : "
                f"si tu le retires maintenant, il ne sera pas rangé.") if arr else ""
        await interaction.followup.send(
            f"Retirer **{name[:150]}** de qBittorrent ?{warn}\n"
            f"• **Garder les fichiers** : le torrent n'est plus partagé, rien n'est effacé.\n"
            f"• **Supprimer les fichiers** : efface aussi ce qui est dans le dossier Téléchargements "
            f"(la copie rangée dans Films/Séries/Anims n'est pas touchée).",
            view=ConfirmRemoveView([h], f"**{name[:80]}**", parent), ephemeral=True)

    async def on_clean(self, interaction: discord.Interaction):
        if not allowed(interaction):
            return await interaction.response.send_message("⛔ Tu n'as pas le rôle requis.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            done = qb_filter(await qb_torrents(), "termines")
            # On ne touche pas à ce que Radarr/Sonarr n'a pas encore rangé
            queued = await queued_hashes()
        except Exception as ex:
            return await interaction.followup.send(f"❌ {ex}", ephemeral=True)
        pending = [t for t in done if t["hash"].upper() in queued]
        ready = [t for t in done if t["hash"].upper() not in queued]
        if not ready:
            return await interaction.followup.send("Aucun torrent terminé à retirer.", ephemeral=True)
        skipped = f"\n({len(pending)} pas encore rangé(s) dans la médiathèque, laissé(s) de côté.)" if pending else ""
        await interaction.followup.send(
            f"Retirer **{len(ready)} torrent(s) terminé(s)** ({size(sum(t['size'] for t in ready))}) "
            f"de qBittorrent ?{skipped}\n"
            f"• **Garder les fichiers** : rien n'est effacé.\n"
            f"• **Supprimer les fichiers** : vide aussi le dossier Téléchargements pour ces torrents "
            f"(les copies rangées dans Films/Séries/Anims ne sont pas touchées).",
            view=ConfirmRemoveView([t["hash"] for t in ready], f"**{len(ready)} torrent(s)**",
                                   (self.origin, self.filtre, self.page)),
            ephemeral=True)



# ---------- Mangas (file de Suwayomi) ----------
MANGA_PAGE = 8


async def mangas_embed(page=0):
    """Renvoie (embed, chapitres en erreur, téléchargeur utile et en marche ?, page corrigée, nombre de pages)."""
    running, groups = await manga.downloads(http)
    pages = max(1, -(-len(groups) // MANGA_PAGE))
    page = min(max(page, 0), pages - 1)
    e = discord.Embed(title="📥 Téléchargements — 📚 Mangas", color=0x2F80ED)
    if not groups:
        e.description = "Aucun chapitre en cours de téléchargement. 🎉"
    for g in groups[page * MANGA_PAGE:(page + 1) * MANGA_PAGE]:
        lines = []
        if g["total"]:
            p = min(g["done"] / g["total"], 1)
            lines.append(f"`{bar(p)}` **{g['done']}/{g['total']} chapitres**")
        if g["current"]:
            lines.append(f"⬇️ {g['current'][:80]} ({g['progress'] * 100:.0f} %)")
        rest = [f"🕒 {g['queued']} en file"] if g["queued"] else []
        if g["errors"]:
            rest.append(f"⚠️ {len(g['errors'])} en erreur")
        if rest:
            lines.append(" · ".join(rest))
        e.add_field(name=g["title"][:250], value="\n".join(lines) or "—", inline=False)
    errors = [c for g in groups for c in g["errors"]]
    pending = sum(g["queued"] + (1 if g["current"] else 0) for g in groups)
    if pending and not running:
        e.description = "⏸️ Le téléchargement des chapitres est à l'arrêt : clique sur 🔁 pour le relancer."
    state = "▶️ en marche" if running else "⏸️ à l'arrêt"
    e.set_footer(text=f"Page {page + 1}/{pages} · {len(groups)} manga(s) · {pending} chapitre(s) à télécharger · "
                      f"{len(errors)} en erreur · téléchargeur {state}")
    return e, errors, running or not pending, page, pages


async def show_mangas(interaction: discord.Interaction, page=0):
    """Affiche (ou rafraîchit) l'onglet Mangas dans le message caché de l'interaction."""
    embed, errors, running, p, pages = await mangas_embed(page)
    await interaction.edit_original_response(embed=embed, view=MangaQueueView(errors, running, p, pages))


def mangas_tab_button(current=False):
    btn = discord.ui.Button(label="📚 Mangas", row=0, disabled=current,
                            style=discord.ButtonStyle.primary if current else discord.ButtonStyle.secondary)

    async def callback(interaction: discord.Interaction):
        if not allowed(interaction):
            return await interaction.response.send_message("⛔ Tu n'as pas le rôle requis.", ephemeral=True)
        await interaction.response.defer()
        try:
            await show_mangas(interaction)
        except Exception as ex:
            await interaction.followup.send(f"❌ Suwayomi : {ex}", ephemeral=True)
    btn.callback = callback
    return btn


class MangaQueueView(BaseView):
    """Onglet Mangas : avancement manga par manga, avec les mêmes onglets que /qb."""
    def __init__(self, errors, running, page=0, pages=1):
        super().__init__(timeout=840)
        self.errors, self.page = errors, page
        for key, label in QB_FILTERS.items():
            btn = discord.ui.Button(label=label, row=0, style=discord.ButtonStyle.secondary)
            btn.callback = self._goto_qb(key)
            self.add_item(btn)
        self.add_item(mangas_tab_button(current=True))

        prev = discord.ui.Button(emoji="◀️", row=1, disabled=page == 0, style=discord.ButtonStyle.secondary)
        prev.callback = self._goto(page - 1)
        nxt = discord.ui.Button(emoji="▶️", row=1, disabled=page >= pages - 1, style=discord.ButtonStyle.secondary)
        nxt.callback = self._goto(page + 1)
        refresh = discord.ui.Button(label="Actualiser", emoji="🔄", row=1, style=discord.ButtonStyle.secondary)
        refresh.callback = self._goto(page)
        for b in (prev, nxt, refresh):
            self.add_item(b)
        if errors or not running:
            label = f"Réessayer les {len(errors)} erreur(s)" if errors else "Relancer"
            retry = discord.ui.Button(label=label, emoji="🔁", row=1, style=discord.ButtonStyle.primary)
            retry.callback = self.on_retry
            self.add_item(retry)

    async def _check(self, interaction):
        if not allowed(interaction):
            await interaction.response.send_message("⛔ Tu n'as pas le rôle requis.", ephemeral=True)
            return False
        await interaction.response.defer()  # répondre à Discord avant le délai de 3 s
        return True

    def _goto(self, page):
        async def callback(interaction: discord.Interaction):
            if not await self._check(interaction):
                return
            try:
                await show_mangas(interaction, page)
            except Exception as ex:
                await interaction.followup.send(f"❌ Suwayomi : {ex}", ephemeral=True)
        return callback

    def _goto_qb(self, filtre):
        async def callback(interaction: discord.Interaction):
            if not await self._check(interaction):
                return
            try:
                embed, chunk, p, pages = await qb_embed(filtre)
                await interaction.edit_original_response(embed=embed, view=QbView(chunk, filtre, p, pages, interaction))
            except Exception as ex:
                await interaction.followup.send(f"❌ {ex}", ephemeral=True)
        return callback

    async def on_retry(self, interaction: discord.Interaction):
        if not await self._check(interaction):
            return
        try:
            await manga.retry_errors(http, self.errors)
            await asyncio.sleep(2)  # laisser Suwayomi reprendre avant de réafficher
            await show_mangas(interaction, self.page)
        except Exception as ex:
            await interaction.followup.send(f"❌ Suwayomi : {ex}", ephemeral=True)

# ---------- Menu de choix ----------
class PickView(BaseView):
    def __init__(self, arr: Arr, results, author_id):
        super().__init__(timeout=120)
        self.arr, self.results, self.author_id = arr, results, author_id
        options = []
        for i, r in enumerate(results):
            already = " (déjà présent)" if r.get("id") else ""
            options.append(discord.SelectOption(
                label=f"{r['title']} ({r.get('year') or '?'}){already}"[:100], value=str(i),
                emoji="🎌" if is_anime(r) else None,
                description=(r.get("overview") or "")[:95] or None))
        select = discord.ui.Select(placeholder="Choisis le bon titre…", options=options)
        select.callback = self.on_pick
        self.add_item(select)

    async def on_pick(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Ce menu n'est pas pour toi.", ephemeral=True)
        await interaction.response.defer()
        item = self.results[int(interaction.data["values"][0])]
        uid, title = interaction.user.id, item["title"]
        if item.get("id"):
            key = sub_key(self.arr, item["id"])
            try:
                relaunched = await relaunch(self.arr, item)
            except Exception as ex:
                return await interaction.edit_original_response(content=f"❌ {ex}", embed=None, view=None)
            if relaunched:
                subscribe(key, title, uid)
                return await announce(
                    interaction, sub_view(self.arr, item["id"]),
                    content=f"🔁 **{title}** était déjà dans {self.arr.name} sans fichier : recherche relancée.\n"
                            f"🔔 {interaction.user.mention} sera prévenu en message privé dès qu'il sera prêt. "
                            f"Les autres peuvent cliquer sur **Me notifier**.")
            if self.arr is sonarr:
                subscribe(key, title, uid)
                return await announce(
                    interaction, sub_view(self.arr, item["id"]),
                    content=f"ℹ️ **{title}** est déjà à jour.\n"
                            f"🔔 {interaction.user.mention} sera prévenu en message privé à chaque nouvel épisode. "
                            f"Les autres peuvent cliquer sur **Me notifier**.")
            return await interaction.edit_original_response(
                content=f"ℹ️ **{title}** est déjà téléchargé dans {self.arr.name}.", embed=None, view=None)
        try:
            created = await self.arr.add(item)
        except Exception as ex:
            return await interaction.edit_original_response(content=f"❌ {ex}", embed=None, view=None)
        what = "chaque nouvel épisode" if self.arr is sonarr else "le film prêt"
        anime_line = ("🎌 Anime : rangé dans **Anims**, VF en priorité, sinon VOSTFR, sinon VOSTEN.\n"
                      if is_anime(item) else "")
        e = discord.Embed(title=f"✅ {title} ({item.get('year', '?')})",
                          description=f"Ajouté à {self.arr.name}, recherche lancée. Avancement : `/qb`.\n"
                                      f"{anime_line}"
                                      f"🔔 {interaction.user.mention} sera prévenu en message privé pour {what}.\n"
                                      f"Les autres peuvent cliquer sur **Me notifier**.",
                          color=0x27AE60)
        poster = next((i.get("remoteUrl") for i in item.get("images", []) if i.get("coverType") == "poster"), None)
        if poster:
            e.set_thumbnail(url=poster)
        e.set_footer(text=f"Demandé par {interaction.user.display_name}")
        view = None
        if created and created.get("id"):
            subscribe(sub_key(self.arr, created["id"]), title, uid)
            view = sub_view(self.arr, created["id"])
        await announce(interaction, view, embed=e)


# ---------- Abonnements aux notifications (qui est prévenu en message privé) ----------
def sub_key(arr, item_id):
    return f"{arr.app}:{int(item_id)}"


def subscribe(key, title, uid):
    s = load_settings()
    entry = s.setdefault("subs", {}).setdefault(key, {"title": title, "users": []})
    entry["title"] = title
    if uid not in entry["users"]:
        entry["users"].append(uid)
    save_settings(s)


def toggle_subscription(key, title, uid):
    """Renvoie True si l'utilisateur est maintenant abonné, False s'il vient de se désabonner."""
    s = load_settings()
    entry = s.setdefault("subs", {}).get(key)
    if entry and uid in entry["users"]:
        entry["users"].remove(uid)
        if not entry["users"]:
            s["subs"].pop(key)
        save_settings(s)
        return False
    save_settings(s)
    subscribe(key, title, uid)
    return True


def drop_subscriptions(key):
    s = load_settings()
    if s.get("subs", {}).pop(key, None) is not None:
        save_settings(s)


def subscribers(key):
    return list(load_settings().get("subs", {}).get(key, {}).get("users", []))


class SubscribeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sub:(?P<app>radarr|sonarr|manga):(?P<id>\d+)"):
    """Bouton permanent (fonctionne même après un redémarrage du bot) : s'abonner / se désabonner."""

    def __init__(self, app, item_id, label="Me notifier", emoji="🔔"):
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                                           custom_id=f"sub:{app}:{int(item_id)}"))
        self.app, self.item_id = app, int(item_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["app"], match["id"])

    async def callback(self, interaction: discord.Interaction):
        arr = APPS[self.app]
        key = f"{self.app}:{self.item_id}"
        title = load_settings().get("subs", {}).get(key, {}).get("title")
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not title:
            try:
                if arr is MANGA:
                    title = await manga.title_of(http, self.item_id)
                else:
                    path = "movie" if arr is radarr else "series"
                    title = (await arr._req("GET", f"{path}/{self.item_id}"))["title"]
            except Exception:
                return await interaction.followup.send("❌ Ce titre n'existe plus.", ephemeral=True)
        if toggle_subscription(key, title, interaction.user.id):
            what = WHAT[self.app]
            msg = f"🔔 Tu seras prévenu en message privé {what} de **{title}**."
        else:
            msg = f"🔕 Tu ne seras plus prévenu pour **{title}**."
        await interaction.followup.send(msg, ephemeral=True)


MANGA = SimpleNamespace(app="manga", name="Suwayomi")
APPS = {"radarr": radarr, "sonarr": sonarr, "manga": MANGA}
WHAT = {"radarr": "quand le film sera prêt", "sonarr": "à chaque nouvel épisode",
        "manga": "à chaque nouveau chapitre"}


def sub_view(arr, item_id, label="Me notifier", emoji="🔔"):
    view = BaseView(timeout=None)
    view.add_item(SubscribeButton(arr.app, item_id, label, emoji))
    return view


class FollowView(BaseView):
    """Menu de /suivre : choisir un titre de la bibliothèque pour s'abonner ou se désabonner."""

    def __init__(self, matches, author_id):
        super().__init__(timeout=120)
        self.matches, self.author_id = matches, author_id
        mine = {k for k, v in load_settings().get("subs", {}).items() if author_id in v.get("users", [])}
        select = discord.ui.Select(placeholder="Choisis le titre…", options=[
            discord.SelectOption(label=f"{m['title']} ({m.get('year') or '?'})"[:100], value=str(i),
                                 emoji={"sonarr": "📺", "radarr": "🎬", "manga": "📚"}[m["_app"]],
                                 description="Déjà suivi — choisir pour arrêter" if m["_key"] in mine else None)
            for i, m in enumerate(matches)])
        select.callback = self.on_pick
        self.add_item(select)

    async def on_pick(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Ce menu n'est pas pour toi.", ephemeral=True)
        m = self.matches[int(interaction.data["values"][0])]
        if toggle_subscription(m["_key"], m["title"], interaction.user.id):
            msg = f"🔔 Tu seras prévenu en message privé {WHAT[m['_app']]} de **{m['title']}**."
        else:
            msg = f"🔕 Tu ne seras plus prévenu pour **{m['title']}**."
        await interaction.response.edit_message(content=msg, view=None)


# ---------- Réglages persistants (salon des notifications) ----------
STATE_FILE = "/state/settings.json"


def load_settings():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(data):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


# ---------- Notifications « prêt à regarder » (webhook Radarr / Sonarr) ----------
def poster_of(obj):
    for img in (obj or {}).get("images", []) or []:
        if img.get("coverType") == "poster":
            return img.get("remoteUrl") or img.get("url")
    return None


def episodes_label(episodes):
    eps = sorted(episodes, key=lambda e: (e.get("seasonNumber", 0), e.get("episodeNumber", 0)))
    if not eps:
        return ""
    first, last = eps[0], eps[-1]
    label = f"S{first.get('seasonNumber', 0):02d}E{first.get('episodeNumber', 0):02d}"
    if len(eps) > 1:
        label += f" → S{last.get('seasonNumber', 0):02d}E{last.get('episodeNumber', 0):02d} ({len(eps)} épisodes)"
    elif first.get("title"):
        label += f" · {first['title']}"
    return label


def build_ready_embed(app, payload):
    event = str(payload.get("eventType", ""))
    if "Download" not in event and "Import" not in event:
        return None  # Test, Grab, Rename… ignorés
    upgrade = payload.get("isUpgrade")
    if app == "radarr" and payload.get("movie"):
        m = payload["movie"]
        quality = ((payload.get("movieFile") or {}).get("quality")) or ""
        e = discord.Embed(title=f"🎬 {m.get('title')} ({m.get('year', '?')})",
                          description=("⬆️ Meilleure version disponible" if upgrade else "Prêt à regarder !"),
                          color=0x27AE60)
    elif app == "sonarr" and payload.get("series"):
        sr = payload["series"]
        files = payload.get("episodeFiles") or ([payload["episodeFile"]] if payload.get("episodeFile") else [])
        quality = (files[0].get("quality") if files else "") or ""
        e = discord.Embed(title=f"📺 {sr.get('title')}",
                          description=f"{episodes_label(payload.get('episodes') or [])}\n"
                                      + ("⬆️ Meilleure version disponible" if upgrade else "Prêt à regarder !"),
                          color=0x8E44AD)
        m = sr
    else:
        return None
    poster = poster_of(m)
    if poster:
        e.set_thumbnail(url=poster)
    if quality:
        e.set_footer(text=str(quality))
    return e


def item_id_of(app, payload):
    obj = payload.get("movie") if app == "radarr" else payload.get("series")
    return (obj or {}).get("id")


LAST_WEBHOOK = {}  # app -> (heure, type d'événement) : sert au diagnostic de /etat


async def handle_webhook(request):
    from aiohttp import web
    import time
    app = request.match_info.get("app", "")
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400)
    event = payload.get("eventType")
    LAST_WEBHOOK[app] = (time.time(), event)
    title = (payload.get("movie") or payload.get("series") or {}).get("title", "")
    print(f"[notif] reçu de {app} : {event} {title}", flush=True)
    embed = build_ready_embed(app, payload)
    item_id = item_id_of(app, payload)
    if embed and item_id:
        asyncio.create_task(dispatch_ready(app, int(item_id), embed, bool(payload.get("isUpgrade"))))
    return web.Response(text="ok")


async def dispatch_ready(app, item_id, embed, upgrade):
    arr = APPS[app]
    key = f"{app}:{item_id}"
    unreachable = []
    # 1. Message privé à chaque abonné (le demandeur + ceux qui ont cliqué « Me notifier »)
    for uid in subscribers(key):
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            view = None if app == "radarr" else sub_view(arr, item_id, "Ne plus me notifier", "🔕")
            await user.send(embed=embed, view=view) if view else await user.send(embed=embed)
        except Exception as ex:
            unreachable.append(uid)
            print(f"[notif] message privé impossible pour {uid} : {ex}", flush=True)
    # 2. Annonce dans le salon choisi avec /notifications (+ mention de ceux qui bloquent les MP)
    channel_id = load_settings().get("notify_channel")
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    if channel:
        try:
            content = " ".join(f"<@{u}>" for u in unreachable) or None
            await channel.send(content=content, embed=embed, view=sub_view(arr, item_id))
        except Exception as ex:
            print(f"[notif] envoi dans le salon impossible : {ex}", flush=True)
    # 3. Un film n'arrive qu'une fois : on vide ses abonnés après le premier téléchargement
    if app == "radarr" and not upgrade:
        drop_subscriptions(key)


async def start_webhook_server():
    from aiohttp import web
    app = web.Application()
    app.router.add_post("/webhook/{app}", handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8000).start()
    print("[notif] serveur de notifications prêt (port 8000)", flush=True)


# ---------- Recherche quotidienne des manquants ----------
SEARCH_HOUR = int(os.getenv("SEARCH_HOUR", "4") or 4)


async def daily_missing_search():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(os.getenv("TZ", "Europe/Paris"))
    except Exception:
        tz = None
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=SEARCH_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        for arr, command in ((radarr, "MissingMoviesSearch"), (sonarr, "MissingEpisodeSearch")):
            try:
                await arr._req("POST", "command", json={"name": command, "monitored": True})
                print(f"[recherche] {arr.name} : recherche des manquants lancée", flush=True)
            except Exception as ex:
                print(f"[recherche] {arr.name} : échec ({ex})", flush=True)
        await asyncio.sleep(60)


# ---------- Mangas : configuration + annonce des nouveaux chapitres ----------
MANGA_POLL = 600  # secondes entre deux vérifications


async def manga_loop():
    await asyncio.sleep(20)
    ready = False
    while True:
        try:
            if not ready:
                ready = await manga.setup(http, lambda m: print(f"[manga] {m}", flush=True))
                print("[manga] Suwayomi configuré", flush=True)
            await check_new_chapters()
        except manga.SuwayomiError as ex:
            print(f"[manga] {ex}", flush=True)
        except Exception as ex:
            print(f"[manga] erreur inattendue : {ex}", flush=True)
        await asyncio.sleep(MANGA_POLL if ready else 60)


async def check_new_chapters():
    st = load_settings()
    counts = st.setdefault("manga_counts", {})
    changed = False
    for mid, title, count, lang, source, busy in await manga.library_progress(http):
        key = str(mid)
        if key not in counts:            # manga ajouté hors du bot : on mémorise sans prévenir
            counts[key] = count
            changed = True
            continue
        if busy or count <= counts[key]:  # téléchargement encore en cours : on attend la fin
            continue
        new = count - counts[key]
        first = counts[key] == 0
        counts[key] = count
        changed = True
        e = discord.Embed(title=f"📚 {title}", color=0xE67E22,
                          description=(f"{count} chapitre(s) prêt(s) à lire !" if first else
                                       f"{new} nouveau(x) chapitre(s) prêt(s) à lire !")
                          + f"\n{manga.FLAG.get(lang, '')} {manga.LABEL.get(lang, lang)} · {source}")
        asyncio.create_task(dispatch_ready("manga", mid, e, False))
    if changed:
        st2 = load_settings()
        st2["manga_counts"] = counts
        save_settings(st2)


def manga_count_reset(mid):
    st = load_settings()
    st.setdefault("manga_counts", {})[str(mid)] = 0
    save_settings(st)


# ---------- Bot ----------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.configured = False

    async def setup_hook(self):
        global http
        http = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                     timeout=aiohttp.ClientTimeout(total=30))
        self.add_dynamic_items(SubscribeButton)  # boutons « Me notifier » toujours actifs
        asyncio.create_task(self.autoconfig_loop())
        asyncio.create_task(daily_missing_search())
        asyncio.create_task(manga_loop())
        await start_webhook_server()

    async def autoconfig_loop(self):
        await asyncio.sleep(15)
        while not self.configured:
            self.configured = await ac.run(lambda m: print(f"[config] {m}", flush=True))
            if not self.configured:
                await asyncio.sleep(30)

    async def sync_guild(self, guild):
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def close(self):
        if http:
            await http.close()
        await super().close()


bot = Bot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if is_expired(error):
        print(f"[discord] /{interaction.command.name if interaction.command else '?'} expirée "
              f"(réponse après 3 s) — ignorée", flush=True)
        return
    import traceback
    traceback.print_exception(error)
    try:
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(f"❌ Erreur inattendue : {str(getattr(error, 'original', error))[:300]}", ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_ready():
    for g in bot.guilds:
        await bot.sync_guild(g)
    import socket
    print(f"[instance] bot lancé sur la machine « {socket.gethostname()} » (pid {os.getpid()})", flush=True)
    print(f"Connecté en tant que {bot.user} — /film /serie /qb /etat prêtes sur "
          f"{len(bot.guilds)} serveur(s)", flush=True)


@bot.event
async def on_guild_join(guild):
    await bot.sync_guild(guild)


def allowed(interaction):
    return not ALLOWED_ROLE or any(r.name == ALLOWED_ROLE for r in getattr(interaction.user, "roles", []))


async def request(interaction, arr: Arr, titre: str):
    if not allowed(interaction):
        return await interaction.response.send_message("⛔ Tu n'as pas le rôle requis.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        results = (await arr.lookup(titre))[:10]
    except Exception as ex:
        return await interaction.followup.send(f"❌ {ex}", ephemeral=True)
    if not results:
        return await interaction.followup.send(f"Aucun résultat pour **{titre}**.", ephemeral=True)
    await interaction.followup.send(f"🔎 Résultats pour **{titre}** :",
                                    view=PickView(arr, results, interaction.user.id), ephemeral=True)


@bot.tree.command(name="film", description="Demander un film (Radarr)")
@app_commands.describe(titre="Titre du film")
async def film(interaction: discord.Interaction, titre: str):
    await request(interaction, radarr, titre)


@bot.tree.command(name="serie", description="Demander une série (Sonarr)")
@app_commands.describe(titre="Titre de la série")
async def serie(interaction: discord.Interaction, titre: str):
    await request(interaction, sonarr, titre)


@bot.tree.command(name="qb", description="Voir les torrents qBittorrent (en cours et terminés) et les retirer")
@app_commands.describe(afficher="Quels torrents afficher (par défaut : en cours)")
@app_commands.choices(afficher=[app_commands.Choice(name=label, value=key) for key, label in QB_FILTERS.items()])
async def qb(interaction: discord.Interaction, afficher: str = "actifs"):
    if not allowed(interaction):
        return await interaction.response.send_message("⛔ Tu n'as pas le rôle requis.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        embed, chunk, page, pages = await qb_embed(afficher)
        await interaction.followup.send(embed=embed, view=QbView(chunk, afficher, page, pages, interaction),
                                        ephemeral=True)
    except Exception as ex:
        await interaction.followup.send(f"❌ {ex}", ephemeral=True)



@bot.tree.command(name="mangas", description="Voir l'avancement des téléchargements de mangas (Suwayomi)")
async def mangas_cmd(interaction: discord.Interaction):
    if not allowed(interaction):
        return await interaction.response.send_message("⛔ Tu n'as pas le rôle requis.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        embed, errors, running, page, pages = await mangas_embed()
        await interaction.followup.send(embed=embed, view=MangaQueueView(errors, running, page, pages),
                                        ephemeral=True)
    except Exception as ex:
        await interaction.followup.send(f"❌ Suwayomi : {ex}", ephemeral=True)

@bot.tree.command(name="notifications", description="Choisir le salon où annoncer les films et épisodes prêts")
@app_commands.describe(activer="Oui : annoncer dans ce salon. Non : arrêter les annonces.")
@app_commands.default_permissions(manage_guild=True)
async def notifications(interaction: discord.Interaction, activer: bool = True):
    settings = load_settings()
    if activer:
        settings["notify_channel"] = interaction.channel_id
        msg = (f"🔔 Les films et épisodes prêts à regarder seront annoncés ici, dans <#{interaction.channel_id}>.\n"
               f"Pense à laisser au bot le droit d'écrire dans ce salon.")
    else:
        settings.pop("notify_channel", None)
        msg = "🔕 Notifications désactivées."
    try:
        save_settings(settings)
    except OSError as ex:
        return await interaction.response.send_message(f"❌ Réglage non enregistré ({ex}).", ephemeral=True)
    await interaction.response.send_message(msg, ephemeral=True)


def _norm(t):
    return ac.norm(t or "")


class MangaPickView(BaseView):
    def __init__(self, groups, author_id):
        super().__init__(timeout=180)
        self.groups, self.author_id = groups, author_id
        options = []
        for i, g in enumerate(groups):
            others = sorted({c["lang"] for c in g["candidates"]} - {g["lang"]})
            extra = f" (+{', '.join(manga.LABEL[l] for l in others)})" if others else ""
            desc = f"{manga.LABEL[g['lang']]} · {g['source']}{extra}"
            if g["in_library"]:
                desc = "déjà suivi · " + desc
            options.append(discord.SelectOption(label=g["title"][:100], value=str(i),
                                                emoji=manga.FLAG.get(g["lang"]), description=desc[:100]))
        select = discord.ui.Select(placeholder="Choisis le manga…", options=options)
        select.callback = self.on_pick
        self.add_item(select)

    async def on_pick(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Ce menu n'est pas pour toi.", ephemeral=True)
        await interaction.response.defer()
        g = self.groups[int(interaction.data["values"][0])]
        await interaction.edit_original_response(
            content=f"⏳ Récupération des chapitres de **{g['title'][:80]}**…", view=None)
        try:
            res = await manga.add(http, g)
        except Exception as ex:
            return await interaction.edit_original_response(content=f"❌ {ex}")
        if res["queued"]:
            manga_count_reset(res["id"])  # annonce quand le premier lot sera téléchargé
        subscribe(f"manga:{res['id']}", res["title"], interaction.user.id)
        fallback = ""
        if res["lang"] != "fr" and any(c["lang"] == "fr" for c in g["candidates"]):
            fallback = "\n(La version française n'avait aucun chapitre disponible : version anglaise prise à la place.)"
        e = discord.Embed(
            title=f"✅ {res['title']}", color=0xE67E22,
            description=(f"{manga.FLAG.get(res['lang'], '')} {manga.LABEL.get(res['lang'], res['lang'])} · "
                         f"{res['source']}\n"
                         f"📚 {res['chapters']} chapitre(s) trouvé(s), {res['queued']} en téléchargement.\n"
                         f"Les nouveaux chapitres arriveront tout seuls dans le dossier des mangas.\n"
                         f"🔔 {interaction.user.mention} sera prévenu en message privé à chaque nouveau chapitre."
                         f"{fallback}"))
        thumb = manga.public_thumbnail(res.get("thumbnail"))
        if thumb:
            e.set_thumbnail(url=thumb)
        e.set_footer(text=f"Demandé par {interaction.user.display_name}")
        await announce(interaction, sub_view(MANGA, res["id"]), embed=e)


@bot.tree.command(name="manga", description="Télécharger un manga / manhwa (VF en priorité, sinon anglais)")
@app_commands.describe(titre="Titre du manga, manhwa ou webtoon")
async def manga_cmd(interaction: discord.Interaction, titre: str):
    if not allowed(interaction):
        return await interaction.response.send_message("⛔ Tu n'as pas le rôle requis.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        groups = await manga.search(http, titre)
    except Exception as ex:
        return await interaction.followup.send(f"❌ {ex}", ephemeral=True)
    if not groups:
        return await interaction.followup.send(f"Aucun manga trouvé pour **{titre}** (ni en VF, ni en anglais).",
                                               ephemeral=True)
    await interaction.followup.send(
        f"🔎 Résultats pour **{titre}** — 🇫🇷 = disponible en VF, 🇬🇧 = en anglais seulement :",
        view=MangaPickView(groups, interaction.user.id), ephemeral=True)


@bot.tree.command(name="suivre", description="Être prévenu en message privé : film, épisode ou chapitre prêt")
@app_commands.describe(titre="Titre d'un film, d'une série ou d'un manga déjà demandé")
async def suivre(interaction: discord.Interaction, titre: str):
    if not allowed(interaction):
        return await interaction.response.send_message("⛔ Tu n'as pas le rôle requis.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    q, matches = _norm(titre), []
    for arr, path in ((sonarr, "series"), (radarr, "movie")):
        try:
            for it in await arr._req("GET", path):
                if q in _norm(it.get("title")):
                    matches.append({"title": it["title"], "year": it.get("year"), "_app": arr.app,
                                    "_key": sub_key(arr, it["id"])})
        except Exception:
            pass
    try:
        for it in await manga.library(http):
            if q in _norm(it.get("title")):
                matches.append({"title": it["title"], "year": None, "_app": "manga", "_key": f"manga:{it['id']}"})
    except Exception:
        pass
    if not matches:
        return await interaction.followup.send(
            f"Aucun film, série ni manga « {titre} » dans la bibliothèque. Demande-le d'abord avec /film ou /serie.",
            ephemeral=True)
    await interaction.followup.send("Pour quel titre veux-tu être prévenu ?",
                                    view=FollowView(matches[:25], interaction.user.id), ephemeral=True)


@bot.tree.command(name="suivis", description="Voir les films et séries pour lesquels tu es prévenu")
async def suivis(interaction: discord.Interaction):
    subs = load_settings().get("subs", {})
    mine = [(k, v["title"]) for k, v in subs.items() if interaction.user.id in v.get("users", [])]
    if not mine:
        return await interaction.response.send_message(
            "Tu ne suis rien pour l'instant. Utilise /suivre ou le bouton 🔔 Me notifier.", ephemeral=True)
    lines = [{"sonarr": "📺 ", "radarr": "🎬 ", "manga": "📚 "}[k.split(":")[0]] + t for k, t in sorted(mine, key=lambda x: x[1].lower())]
    await interaction.response.send_message(
        "**Tu es prévenu en message privé pour :**\n" + "\n".join(lines)
        + "\n\nPour arrêter : /suivre avec le titre, puis choisis-le à nouveau.", ephemeral=True)


@bot.tree.command(name="help", description="Voir toutes les commandes du bot")
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(title="🏴‍☠️ Commandes du bot", color=0x23A0B3,
                      description="Demande un film ou une série, le bot s'occupe du reste.")
    e.add_field(name="🎬 Demander", inline=False, value=(
        "`/film titre:Dune` — choisis le film dans la liste : il est cherché, téléchargé puis rangé.\n"
        "`/serie titre:The Bear` — pareil pour une série ; les nouveaux épisodes arrivent tout seuls.\n"
        "🎌 Les animes sont reconnus tout seuls : rangés dans **Anims**, en VF si possible, "
        "sinon VOSTFR, sinon VOSTEN (et remplacés si une meilleure version sort)."))
    e.add_field(name="📚 Mangas / manhwas", inline=False, value=(
        "`/manga titre:Solo Leveling` — en VF si une source française l'a, sinon en anglais. "
        "Tous les chapitres sont téléchargés dans le dossier des mangas, puis chaque nouveau chapitre dès sa sortie."))
    e.add_field(name="📥 Suivre les téléchargements", inline=False, value=(
        "`/qb` — avancement, vitesse, temps restant et seeders. Les onglets **En cours / Terminés / Tous** "
        "listent tous les torrents de qBittorrent.\n"
        "Son menu **Retirer ou annuler un torrent…** : un téléchargement en cours peut être annulé "
        "ou remplacé par une autre version, un torrent terminé retiré (avec ou sans ses fichiers). "
        "🧹 retire d'un coup tous les terminés déjà rangés.\n"
        "`/mangas` (ou l'onglet **📚 Mangas** de `/qb`) — chapitres téléchargés / total pour chaque manga, "
        "chapitre en cours, file d'attente, et 🔁 pour réessayer les chapitres en erreur."))
    e.add_field(name="🔔 Être prévenu", inline=False, value=(
        "Celui qui fait la demande reçoit un message privé quand c'est prêt.\n"
        "`/suivre titre:…` ou le bouton **Me notifier** — être prévenu aussi (re-cliquer pour arrêter).\n"
        "`/suivis` — la liste de tes abonnements.\n"
        "`/notifications` — annoncer tout ce qui arrive dans ce salon (réservé aux gérants du serveur)."))
    e.add_field(name="🩺 Vérifier", inline=False, value=(
        "`/etat` — vérifie que tout fonctionne (Radarr, Sonarr, Jackett, IPTorrents, qBittorrent)."))
    e.set_footer(text=f"Versions en VO privilégiées · minimum {ac.MIN_SEEDERS} seeders · "
                      f"films et épisodes manquants recherchés chaque nuit à {SEARCH_HOUR} h")
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="etat", description="Vérifier que toute l'installation fonctionne")
async def etat(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    lines = []

    async def check(label, coro):
        try:
            res = await coro
            lines.append(f"✅ **{label}** {res or ''}")
        except Exception as ex:
            lines.append(f"❌ **{label}** — {str(ex)[:150]}")

    async def arr_state(arr):
        roots = await arr._req("GET", "rootfolder")
        clients = await arr._req("GET", "downloadclient")
        idx = await arr._req("GET", "indexer")
        miss = [n for n, ok in (("dossier", roots), ("qBittorrent", clients), ("Jackett", idx)) if not ok]
        if miss:
            raise RuntimeError("pas encore branché : " + ", ".join(miss))
        has_anime = any(ac.norm(r["path"].rstrip("/").rsplit("/", 1)[-1]) in ac.ANIME_DIR_ALIASES for r in roots)
        profiles = [p["name"] for p in await arr._req("GET", "qualityprofile")]
        if not has_anime or ac.ANIME_PROFILE not in profiles:
            raise RuntimeError("animes pas encore prêts (dossier Anims ou profil Anime manquant), "
                               "réessaie dans une minute")
        return "— " + " · ".join(r["path"] for r in roots)

    async def jackett_state():
        ids = await ac.jackett_status()
        missing_anime = [i for i in ac.ANIME_INDEXERS if i not in ids]
        if missing_anime:
            raise RuntimeError(f"trackers d'anime pas encore ajoutés ({', '.join(missing_anime)}), "
                               f"réessaie dans une minute")
        if ac.ipt_wanted() and "iptorrents" not in ids:
            raise RuntimeError("IPTorrents : " + ac.last_errors.get(
                "iptorrents", "pas encore configuré, réessaie dans une minute"))
        if not ids:
            raise RuntimeError("aucun indexeur : remplis IPT_COOKIE / IPT_USERAGENT puis redéploie")
        return f"— {len(ids)} indexeur(s) : {', '.join(ids)}"

    async def qb_state():
        t = await qb_torrents()
        return f"— {len(t)} torrent(s)"

    await check("Radarr", arr_state(radarr))
    await check("Sonarr", arr_state(sonarr))
    await check("Jackett", jackett_state())
    await check("qBittorrent", qb_state())

    async def notif_state():
        st = load_settings()
        ch = st.get("notify_channel")
        n = sum(len(v.get("users", [])) for v in st.get("subs", {}).values())
        where = f"annonces dans <#{ch}>" if ch else "pas de salon d'annonces (facultatif : /notifications)"
        return f"— messages privés : {n} abonnement(s) · {where} · manquants recherchés chaque jour à {SEARCH_HOUR} h"

    await check("Notifications", notif_state())

    async def manga_state():
        st = await manga.status(http)
        if not st["fr"] and not st["en"]:
            raise RuntimeError("aucune source installée pour l'instant, réessaie dans quelques minutes")
        if not st["settings_ok"]:
            raise RuntimeError("téléchargement automatique pas encore activé, réessaie dans une minute")
        extra = f" · ⚠️ {st['errors']} chapitre(s) en erreur" if st["errors"] else ""
        return (f"— {st['fr']} source(s) FR · {st['en']} source(s) EN · "
                f"{st['pending']} chapitre(s) en attente{extra}")

    await check("Mangas (Suwayomi)", manga_state())

    async def webhook_state(arr):
        """Vérifie que Radarr/Sonarr savent joindre le bot, en déclenchant un test réel."""
        import time
        hooks = [n for n in await arr._req("GET", "notification") if n.get("name") == ac.WEBHOOK_NAME]
        if not hooks:
            await ac.setup_webhook(arr.name, arr.url, ac.arr_key(arr.app), lambda m: None)
            hooks = [n for n in await arr._req("GET", "notification") if n.get("name") == ac.WEBHOOK_NAME]
            if not hooks:
                raise RuntimeError("liaison vers le bot absente et impossible à créer")
        started = time.time()
        try:
            await arr._req("POST", "notification/test", json=hooks[0])
        except Exception as ex:
            raise RuntimeError(f"{arr.name} n'arrive pas à joindre le bot ({str(ex)[:120]})")
        for _ in range(20):
            if LAST_WEBHOOK.get(arr.app, (0,))[0] >= started:
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError(f"le test de {arr.name} n'est jamais arrivé au bot")
        return "— liaison vers le bot OK"

    async def import_state(arr):
        """Téléchargements finis mais bloqués avant le rangement (droits, fichier non reconnu…)."""
        extra = {"includeMovie": "true"} if arr is radarr else {"includeSeries": "true"}
        q = await arr._req("GET", "queue", params={"pageSize": 200, **extra})
        stuck = [r for r in q.get("records", [])
                 if r.get("trackedDownloadState") in ("importPending", "importBlocked", "failedPending")
                 or r.get("trackedDownloadStatus") in ("warning", "error")]
        if not stuck:
            return "— aucun téléchargement bloqué"
        r = stuck[0]
        msgs = [m for sm in r.get("statusMessages", []) for m in (sm.get("messages") or [sm.get("title", "")])]
        why = (msgs[0] if msgs else r.get("errorMessage") or r.get("trackedDownloadState", "?"))[:160]
        raise RuntimeError(f"{len(stuck)} fini(s) mais pas rangé(s). Ex. « {r.get('title', '?')[:60]} » : {why}")

    await check("Radarr → bot", webhook_state(radarr))
    await check("Sonarr → bot", webhook_state(sonarr))
    await check("Rangement Radarr", import_state(radarr))
    await check("Rangement Sonarr", import_state(sonarr))
    color = 0x27AE60 if all(l.startswith("✅") for l in lines) else 0xE67E22
    await interaction.followup.send(embed=discord.Embed(title="🩺 État de l'installation",
                                                        description="\n".join(lines), color=color),
                                    ephemeral=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN or "COLLE_ICI" in DISCORD_TOKEN:
        raise SystemExit("⚠️  Colle le token du bot dans DISCORD_TOKEN (docker-compose.yml) puis redéploie.")
    bot.run(DISCORD_TOKEN)
