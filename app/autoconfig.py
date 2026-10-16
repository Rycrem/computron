"""
Configuration automatique de la stack, lancée par le bot au démarrage.
Tout est idempotent : rien n'est ajouté deux fois.

  - lit les clés API générées par Radarr, Sonarr et Jackett dans leurs fichiers de config
  - Jackett  : débranche l'ancien FlareSolverr + ajoute les indexeurs listés dans JACKETT_INDEXERS
  - Radarr   : dossier /data/Films, client qBittorrent, un indexeur par tracker Jackett
  - Sonarr   : dossier /data/Séries, client qBittorrent, un indexeur par tracker Jackett
"""
import json
import os
import re
import unicodedata

import aiohttp

RADARR = "http://radarr:7878"
SONARR = "http://sonarr:8989"
JACKETT = "http://jackett:9117"
QBIT = "http://qbittorrent:8080"
CFG = "/cfg"
DATA = "/data"


def norm(s):
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


def media_dir(aliases, default):
    """Nom réel du dossier (gère Séries/Series, accents, majuscules)."""
    try:
        for name in os.listdir(DATA):
            if os.path.isdir(os.path.join(DATA, name)) and norm(name) in aliases:
                return f"{DATA}/{name}"
    except FileNotFoundError:
        pass
    return f"{DATA}/{default}"


def arr_key(app):
    try:
        with open(f"{CFG}/{app}/config.xml", encoding="utf-8") as f:
            m = re.search(r"<ApiKey>([^<]+)</ApiKey>", f.read())
            return m.group(1) if m else None
    except FileNotFoundError:
        return None


def jackett_key():
    try:
        with open(f"{CFG}/jackett/Jackett/ServerConfig.json", encoding="utf-8-sig") as f:
            return json.load(f).get("APIKey")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def qbit_password():
    try:
        with open(f"{CFG}/qbittorrent/MOT_DE_PASSE_QBITTORRENT.txt", encoding="utf-8") as f:
            m = re.search(r"Mot de passe : (\S+)", f.read())
            return m.group(1) if m else ""
    except FileNotFoundError:
        return ""


def jackett_indexers_wanted():
    raw = os.getenv("JACKETT_INDEXERS", "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


IPT_COOKIE = os.getenv("IPT_COOKIE", "").strip()
IPT_USERAGENT = os.getenv("IPT_USERAGENT", "").strip()
last_errors = {}  # affichées par /etat


def ipt_wanted():
    return bool(IPT_COOKIE) and "COLLE_ICI" not in IPT_COOKIE


async def setup_iptorrents(s, log):
    """IPTorrents se connecte avec le cookie + User-Agent du navigateur (pas de mot de passe)."""
    conf = await _json(s, "GET", f"{JACKETT}/api/v2.0/indexers/iptorrents/config")
    for item in conf:
        key = f"{item.get('id', '')} {item.get('name', '')}".lower()
        if "cookie" in key:
            item["value"] = IPT_COOKIE
        elif "agent" in key:
            item["value"] = IPT_USERAGENT
    try:
        # Jackett teste la connexion au site avant d'enregistrer
        await _json(s, "POST", f"{JACKETT}/api/v2.0/indexers/iptorrents/config", json=conf)
        last_errors.pop("iptorrents", None)
        log("Jackett : IPTorrents configuré et connexion réussie")
    except Exception as ex:
        last_errors["iptorrents"] = ("connexion refusée par IPTorrents : cookie expiré ou User-Agent "
                                     "différent de celui du navigateur")
        log(f"Jackett : IPTorrents refusé ({ex})")


def why(ex):
    """Texte lisible d'une erreur (certaines, comme un délai dépassé, n'ont pas de message)."""
    return str(ex).strip() or {"TimeoutError": "pas de réponse à temps (l'appli démarre encore ?)"}.get(
        type(ex).__name__, type(ex).__name__)


async def _json(session, method, url, **kw):
    async with session.request(method, url, **kw) as r:
        if r.status >= 400:
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {r.status} {(await r.text())[:200]}")
        txt = await r.text()
        return json.loads(txt) if txt.strip() else None


# ---------------- Jackett ----------------
async def jackett_session():
    """Jackett sans mot de passe admin : visiter le tableau de bord donne le cookie de session."""
    s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                              timeout=aiohttp.ClientTimeout(total=60))
    try:
        async with s.get(f"{JACKETT}/UI/Dashboard") as r:
            await r.read()
    except BaseException:
        await s.close()  # Jackett pas encore démarré : ne pas laisser la session ouverte
        raise
    return s


async def setup_jackett(log):
    s = await jackett_session()
    try:
        cfg = await _json(s, "GET", f"{JACKETT}/api/v2.0/server/config")
        # FlareSolverr a été retiré du stack (Chromium plante sur le NAS ARM) : on le débranche
        if isinstance(cfg, dict) and cfg.get("flaresolverrurl") == "http://flaresolverr:8191":
            cfg["flaresolverrurl"] = ""
            await _json(s, "POST", f"{JACKETT}/api/v2.0/server/config", json=cfg)
            log("Jackett : FlareSolverr débranché")

        configured = {i["id"] for i in await _json(s, "GET", f"{JACKETT}/api/v2.0/indexers?configured=true")}
        for idx in jackett_indexers_wanted():
            if idx in configured:
                continue
            try:
                conf = await _json(s, "GET", f"{JACKETT}/api/v2.0/indexers/{idx}/config")
                await _json(s, "POST", f"{JACKETT}/api/v2.0/indexers/{idx}/config", json=conf)
                log(f"Jackett : indexeur « {idx} » ajouté")
            except Exception as ex:
                log(f"Jackett : impossible d'ajouter « {idx} » ({ex})")

        for idx, settings in ANIME_INDEXERS.items():
            await setup_public_indexer(s, idx, settings, configured, log)

        if ipt_wanted():
            await setup_iptorrents(s, log)
    finally:
        await s.close()


# Trackers d'anime publics (aucun compte) ajoutés automatiquement
ANIME_INDEXERS = {
    # nekoBT : ajoute les langues audio / sous-titres au titre ({Tags:…A=ja;S=en,fr;})
    "nekobt": {"incltags": True},
    # Nyaa.si : le plus gros catalogue (surtout VOSTEN) ; numéros de saison ajoutés pour Sonarr
    "nyaasi": {"sonarr_compatibility": True, "radarr_compatibility": True},
}


async def setup_public_indexer(s, idx, settings, configured, log):
    try:
        conf = await _json(s, "GET", f"{JACKETT}/api/v2.0/indexers/{idx}/config")
        changed = idx not in configured
        for item in conf:
            if item.get("id") in settings and item.get("value") != settings[item["id"]]:
                item["value"] = settings[item["id"]]
                changed = True
        if changed:
            await _json(s, "POST", f"{JACKETT}/api/v2.0/indexers/{idx}/config", json=conf)
            log(f"Jackett : tracker d'anime « {idx} » configuré")
    except Exception as ex:
        log(f"Jackett : impossible d'ajouter « {idx} » ({ex})")


async def jackett_status():
    s = await jackett_session()
    try:
        return [i["id"] for i in await _json(s, "GET", f"{JACKETT}/api/v2.0/indexers?configured=true")]
    finally:
        await s.close()


# ---------------- Radarr / Sonarr ----------------
async def setup_arr(name, url, key, root_list, category_field, category, jkey, jackett_ids, log):
    h = {"X-Api-Key": key}
    async with aiohttp.ClientSession(headers=h, timeout=aiohttp.ClientTimeout(total=60)) as s:
        # Dossiers racines (Films ou Séries, + Anims)
        roots = await _json(s, "GET", f"{url}/api/v3/rootfolder")
        for root in root_list:
            if not any(r["path"].rstrip("/") == root for r in roots):
                await _json(s, "POST", f"{url}/api/v3/rootfolder", json={"path": root})
                log(f"{name} : dossier {root} ajouté")

        # Client qBittorrent
        clients = await _json(s, "GET", f"{url}/api/v3/downloadclient")
        if not any(c.get("implementation") == "QBittorrent" for c in clients):
            schema = next(x for x in await _json(s, "GET", f"{url}/api/v3/downloadclient/schema")
                          if x["implementation"] == "QBittorrent")
            values = {"host": "qbittorrent", "port": 8080, "useSsl": False,
                      "username": "admin", "password": qbit_password(), category_field: category}
            for f in schema["fields"]:
                if f["name"] in values:
                    f["value"] = values[f["name"]]
            schema.update({"name": "qBittorrent", "enable": True, "priority": 1})
            await _json(s, "POST", f"{url}/api/v3/downloadclient?forceSave=true", json=schema)
            log(f"{name} : qBittorrent branché")

        # Indexeurs Jackett : un flux par tracker. Le flux agrégé « all » mélange les capacités
        # des trackers : une recherche par identifiant TVDB/IMDb renvoie alors les derniers
        # torrents de nekoBT / Nyaa (sans rapport avec la série), Sonarr les rejette tous et
        # ne relance jamais la recherche par titre sur IPTorrents.
        indexers = await _json(s, "GET", f"{url}/api/v3/indexer")
        have = {jackett_url(i) for i in indexers}
        schema, missing = None, []
        for idx in jackett_ids:
            feed = f"{JACKETT}/api/v2.0/indexers/{idx}/results/torznab"
            if feed in have:
                continue
            if schema is None:
                schema = next(x for x in await _json(s, "GET", f"{url}/api/v3/indexer/schema")
                              if x["implementation"] == "Torznab")
            conf = json.loads(json.dumps(schema))
            values = {"baseUrl": feed, "apiPath": "/api", "apiKey": jkey}
            for f in conf["fields"]:
                if f["name"] in values:
                    f["value"] = values[f["name"]]
            conf.update({"name": f"Jackett - {idx}", "enableRss": True, "enableAutomaticSearch": True,
                         "enableInteractiveSearch": True, "priority": 25})
            try:  # Sonarr/Radarr testent le tracker avant d'enregistrer : un tracker en panne ne bloque pas les autres
                await _json(s, "POST", f"{url}/api/v3/indexer?forceSave=true", json=conf)
                have.add(feed)
                log(f"{name} : tracker « {idx} » branché (via Jackett)")
            except Exception as ex:
                missing.append(idx)
                log(f"{name} : tracker « {idx} » pas encore branché ({why(ex)})")
        if any(h and "/indexers/all/" not in h for h in have):  # remplacé par les flux par tracker
            for i in indexers:
                if "/indexers/all/" in jackett_url(i):
                    await _json(s, "DELETE", f"{url}/api/v3/indexer/{i['id']}")
                    log(f"{name} : ancien flux Jackett « tous les indexeurs » retiré")
        return missing


def jackett_url(indexer):
    """URL du flux Torznab d'un indexeur Radarr/Sonarr ('' si ce n'est pas un flux Jackett)."""
    for f in indexer.get("fields") or []:
        if f.get("name") == "baseUrl" and "jackett:9117" in str(f.get("value")):
            return str(f["value"]).rstrip("/")
    return ""


# ---------------- Notifications vers le bot ----------------
WEBHOOK_NAME = "Bot Discord"
BOT_WEBHOOK = "http://arr-bot:8000/webhook"


async def setup_webhook(name, url, key, log):
    """Radarr/Sonarr préviennent le bot dès qu'un fichier est importé."""
    h = {"X-Api-Key": key}
    async with aiohttp.ClientSession(headers=h, timeout=aiohttp.ClientTimeout(total=60)) as s:
        existing = await _json(s, "GET", f"{url}/api/v3/notification")
        if any(n.get("name") == WEBHOOK_NAME for n in existing):
            return
        schema = next(x for x in await _json(s, "GET", f"{url}/api/v3/notification/schema")
                      if x["implementation"] == "Webhook")
        for f in schema["fields"]:
            if f["name"] == "url":
                f["value"] = f"{BOT_WEBHOOK}/{name.lower()}"
            elif f["name"] == "method":
                f["value"] = 1  # POST
        schema["name"] = WEBHOOK_NAME
        # Sonarr récent : « import terminé » = 1 message par téléchargement (saison complète incluse)
        if "onImportComplete" in schema:
            schema.update({"onImportComplete": True, "onDownload": False, "onUpgrade": False})
        else:
            schema.update({"onDownload": True, "onUpgrade": True})
        await _json(s, "POST", f"{url}/api/v3/notification?forceSave=true", json=schema)
        log(f"{name} : notifications vers le bot activées")


# ---------------- Préférences : VO + seeders ----------------
MIN_SEEDERS = int(os.getenv("MIN_SEEDERS", "3") or 3)
PREFERRED_PROFILE = "HD-1080p"

CF_VO = "VO (langue originale)"
CF_VF_ONLY = "Doublé FR uniquement"
CUSTOM_FORMATS = {
    # +score : la langue parlée détectée est la langue originale du film / de la série
    CF_VO: (150, [{"name": "Langue originale", "implementation": "LanguageSpecification",
                   "negate": False, "required": True, "fields": [{"name": "value", "value": -2}]}]),
    # -score : titre marqué VF/FRENCH sans MULTI ni VOSTFR = pas de piste originale -> rejeté
    CF_VF_ONLY: (-10000, [
        {"name": "Marqué VF", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True,
         "fields": [{"name": "value", "value": r"\b(TRUEFRENCH|FRENCH|VFF|VFQ|VFI|VF2?)\b"}]},
        {"name": "Sans piste VO", "implementation": "ReleaseTitleSpecification", "negate": True, "required": True,
         "fields": [{"name": "value", "value": r"\b(MULTI|VOSTFR|VO)\b"}]},
    ]),
}


# ---------------- Anime : VF > VOSTFR > multi-sous-titres > VOSTEN ----------------
ANIME_PROFILE = "Anime"
ANIME_DIR_ALIASES = {"anims", "anime", "animes"}
_TAG_LANG = r"\{{Tags:[^}}]*\b{kind}=([a-z]+,)*{lang}[,;]"   # balises nekoBT
ANIME_FORMATS = {
    "Anime : VF": (1000, [
        {"name": "VF", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True,
         "fields": [{"name": "value", "value":
                     r"\b(VFF|VFQ|VFI|VF2?|TRUEFRENCH|FRENCH)\b|\bMULTI\b(?![ ._-]?(SUBS?|SUBTITLES?|AUDIO)\b)|"
                     + _TAG_LANG.format(kind="A", lang="fr(ca)?")}]}]),
    "Anime : VOSTFR": (500, [
        {"name": "VOSTFR", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True,
         "fields": [{"name": "value", "value":
                     r"\b(VOSTFR|SUBFRENCH)\b|" + _TAG_LANG.format(kind="S", lang="fr(ca)?")}]}]),
    "Anime : multi-sous-titres": (150, [
        {"name": "Multi-subs", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True,
         "fields": [{"name": "value", "value": r"\b(MULTI[ ._-]?SUBS?|Multiple[ ._-]?Subtitles?)\b"}]}]),
    "Anime : VOSTEN": (100, [
        {"name": "VOSTEN", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True,
         "fields": [{"name": "value", "value": r"\b(VOSTEN|ENG[ ._-]?SUBS?)\b|" + _TAG_LANG.format(kind="S", lang="en")}]}]),
    "Anime : RAW ou autre langue": (-10000, [
        {"name": "RAW / autre langue", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True,
         "fields": [{"name": "value", "value":
                     r"(?<!Erai-)\bRAWS?\b|\b(VOSTA|SUB[ ._-]?ESP|LEGENDADO|DUBLADO|CASTELLANO|PT[ ._-]?BR)\b"}]}]),
    "Anime : doublage anglais seul": (-10000, [
        {"name": "Doublage anglais", "implementation": "ReleaseTitleSpecification", "negate": False, "required": True,
         "fields": [{"name": "value", "value": r"\b(ENG(LISH)?[ ._-]?DUB(BED)?|DUBBED)\b"}]},
        {"name": "Sans VO ni sous-titres", "implementation": "ReleaseTitleSpecification", "negate": True,
         "required": True,
         "fields": [{"name": "value", "value": r"\b(DUAL|MULTI|SUBS?|SUBBED|VOSTFR|VF)\b"}]}]),
}
# Dans le profil Anime, les règles « VO » des films/séries classiques ne s'appliquent pas
ANIME_PROFILE_SCORES = {**{n: 0 for n in CUSTOM_FORMATS}, **{n: sc for n, (sc, _) in ANIME_FORMATS.items()}}
ANIME_CUTOFF = 1000  # VF atteinte : plus d'amélioration recherchée


async def ensure_formats(s, url, formats, name, log):
    existing = {cf["name"]: cf["id"] for cf in await _json(s, "GET", f"{url}/api/v3/customformat")}
    for cf_name, (_, specs) in formats.items():
        if cf_name not in existing:
            cf = await _json(s, "POST", f"{url}/api/v3/customformat",
                             json={"name": cf_name, "includeCustomFormatWhenRenaming": False,
                                   "specifications": specs})
            existing[cf_name] = cf["id"]
            log(f"{name} : format « {cf_name} » créé")
    return existing


async def setup_anime_profile(s, name, url, existing, log):
    profiles = await _json(s, "GET", f"{url}/api/v3/qualityprofile")
    anime = next((p for p in profiles if p["name"] == ANIME_PROFILE), None)
    if not anime:
        base = next((p for p in profiles if p["name"] == PREFERRED_PROFILE), profiles[0])
        anime = {k: v for k, v in base.items() if k != "id"}
        anime["name"] = ANIME_PROFILE
        created = True
    else:
        created = False
    ids = {existing[n]: sc for n, sc in ANIME_PROFILE_SCORES.items() if n in existing}
    for item in anime.get("formatItems", []):
        if item.get("format") in ids:
            item["score"] = ids[item["format"]]
    anime.update({"minFormatScore": 0, "upgradeAllowed": True, "cutoffFormatScore": ANIME_CUTOFF})
    if "language" in anime:
        anime["language"] = {"id": -1, "name": "Any"}
    if created:
        await _json(s, "POST", f"{url}/api/v3/qualityprofile", json=anime)
        log(f"{name} : profil « {ANIME_PROFILE} » créé (VF > VOSTFR > VOSTEN)")
    else:
        await _json(s, "PUT", f"{url}/api/v3/qualityprofile/{anime['id']}", json=anime)


async def setup_preferences(name, url, key, log):
    h = {"X-Api-Key": key}
    async with aiohttp.ClientSession(headers=h, timeout=aiohttp.ClientTimeout(total=60)) as s:
        # 1. Formats personnalisés (créés une seule fois)
        existing = await ensure_formats(s, url, {**CUSTOM_FORMATS, **ANIME_FORMATS}, name, log)

        # 2. Scores dans tous les profils qualité (+ langue « Toutes » pour ne pas bloquer la VO)
        for p in await _json(s, "GET", f"{url}/api/v3/qualityprofile"):
            if p["name"] == ANIME_PROFILE:
                continue  # réglé à part, juste en dessous
            changed = False
            for item in p.get("formatItems", []):
                for cf_name, (score, _) in CUSTOM_FORMATS.items():
                    if item.get("format") == existing[cf_name] and item.get("score") != score:
                        item["score"] = score
                        changed = True
            if "language" in p and isinstance(p["language"], dict) and p["language"].get("id") not in (-1, -2):
                p["language"] = {"id": -1, "name": "Any"}
                changed = True
            if p.get("minFormatScore", 0) != 0:
                p["minFormatScore"] = 0
                changed = True
            if changed:
                await _json(s, "PUT", f"{url}/api/v3/qualityprofile/{p['id']}", json=p)
        log(f"{name} : préférence VO appliquée")
        await setup_anime_profile(s, name, url, existing, log)

        # 3. Minimum de seeders sur l'indexeur Jackett
        for idx in await _json(s, "GET", f"{url}/api/v3/indexer"):
            if "jackett:9117" not in str(idx.get("fields")):
                continue
            wanted = {"minimumSeeders": MIN_SEEDERS, "animeCategories": [5070], "animeStandardFormatSearch": True}
            changed = False
            for f in idx["fields"]:
                if f["name"] in wanted and f.get("value") != wanted[f["name"]]:
                    f["value"] = wanted[f["name"]]
                    changed = True
            if changed:
                await _json(s, "PUT", f"{url}/api/v3/indexer/{idx['id']}?forceSave=true", json=idx)
                log(f"{name} : indexeur Jackett réglé (seeders min. {MIN_SEEDERS}, catégories anime)")


async def run(log=print):
    """Renvoie True quand tout est configuré, False s'il faut réessayer plus tard."""
    rk, sk, jk = arr_key("radarr"), arr_key("sonarr"), jackett_key()
    if not (rk and sk and jk):
        log("En attente du premier démarrage de Radarr, Sonarr et Jackett…")
        return False
    try:
        await setup_jackett(log)
    except Exception as ex:  # non bloquant : Radarr/Sonarr peuvent être configurés quand même
        log(f"Jackett : réglages ignorés pour l'instant ({why(ex)})")
    try:
        jackett_ids = await jackett_status()
    except Exception as ex:
        jackett_ids = []
        log(f"Jackett : liste des trackers indisponible ({why(ex)})")
    try:
        anims = media_dir(ANIME_DIR_ALIASES, "Anims")
        missing = await setup_arr("Radarr", RADARR, rk, [media_dir({"films"}, "Films"), anims],
                                  "movieCategory", "radarr", jk, jackett_ids, log)
        missing += await setup_arr("Sonarr", SONARR, sk, [media_dir({"series"}, "Séries"), anims],
                                   "tvCategory", "tv-sonarr", jk, jackett_ids, log)
    except Exception as ex:
        log(f"Configuration pas encore possible : {why(ex)}")
        return False
    for name, url, key in (("Radarr", RADARR, rk), ("Sonarr", SONARR, sk)):
        try:
            await setup_preferences(name, url, key, log)
        except Exception as ex:  # non bloquant
            log(f"{name} : préférences VO/seeders non appliquées ({why(ex)})")
        try:
            await setup_webhook(name, url, key, log)
        except Exception as ex:  # non bloquant
            log(f"{name} : notifications non activées ({why(ex)})")
    if not jackett_ids or missing:
        log("Trackers Jackett pas encore tous branchés à Radarr et Sonarr, nouvel essai bientôt…")
        return False
    log("Configuration automatique terminée ✔")
    return True
