"""
Mangas / manhwas via Suwayomi (téléchargement chapitre par chapitre depuis des sources en ligne).

  - configure Suwayomi au démarrage : extensions FR + EN, téléchargement automatique des
    nouveaux chapitres (même non lus : la lecture se fait dans une autre appli), CBZ, MAJ toutes les 6 h
  - recherche : sources françaises d'abord, anglaises ensuite (VF prioritaire, sinon VO anglaise)
  - suivi : ajout à la bibliothèque + téléchargement de tous les chapitres existants
Les fichiers arrivent dans MANGA_DIR/<Source>/<Titre>/<Chapitre>.cbz
"""
import asyncio
import re
import unicodedata

import aiohttp

GRAPHQL = "http://suwayomi:4567/api/graphql"
PREFIX = "eu.kanade.tachiyomi.extension."

# Extensions installées automatiquement, dans l'ordre de préférence (à langue égale)
EXTENSIONS = [
    "all.mangadex",          # MangaDex : FR + EN, le plus fiable
    "all.webtoons",          # Webtoons.com : webtoons officiels FR + EN
    "fr.sushiscan",          # Sushi-Scan : mangas / manhwas FR
    "fr.animesama",          # Anime-Sama : gros catalogue FR
    "fr.mangasoriginesfr",   # Mangas-Origines : manhwas FR
    "fr.raijinscans",        # Raijin Scans : manhwas FR
    "fr.japscan",            # Japscan : gros catalogue FR (parfois protégé, passe par FlareSolverr)
    "en.weebcentral",        # Weeb Central : gros catalogue manga EN
    "en.asurascans",         # Asura Scans : manhwas EN
    "en.flamecomics",        # Flame Comics : manhwas EN
    "all.mangafire",         # MangaFire : FR + EN, en dernier recours
]
LANGS = ["fr", "en"]          # ordre de priorité des langues
FLAG = {"fr": "🇫🇷", "en": "🇬🇧"}
LABEL = {"fr": "VF", "en": "anglais"}

SETTINGS = {
    "autoDownloadNewChapters": True,         # nouveaux chapitres téléchargés tout seuls
    "excludeEntryWithUnreadChapters": False,  # même si rien n'est « lu » dans Suwayomi
    "autoDownloadIgnoreReUploads": True,
    "excludeUnreadChapters": False,           # vérifier les MAJ même si non lu
    "excludeNotStarted": False,
    "excludeCompleted": True,                 # inutile de vérifier les séries terminées
    "downloadAsCbz": True,
}


class SuwayomiError(RuntimeError):
    pass


async def gql(http, query, variables=None, timeout=60):
    try:
        async with http.post(GRAPHQL, json={"query": query, "variables": variables or {}},
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status >= 400:
                raise SuwayomiError(f"Suwayomi a répondu {r.status}")
            data = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
        raise SuwayomiError(f"Suwayomi injoignable ({type(ex).__name__})")
    if data.get("errors"):
        raise SuwayomiError(data["errors"][0].get("message", "erreur inconnue")[:200])
    return data["data"]


def norm(t):
    t = unicodedata.normalize("NFKD", (t or "").casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


# ---------------- Configuration automatique ----------------
async def setup(http, log=print):
    """Renvoie True quand Suwayomi est prêt (réglages + extensions installées)."""
    await gql(http, "query { aboutServer { version } }", timeout=15)
    # réglages écrits en clair dans la requête (pas de dépendance au nom du type d'entrée)
    literal = ", ".join(f"{k}: {'true' if v else 'false'}" for k, v in SETTINGS.items())
    await gql(http, f"mutation {{ setSettings(input: {{settings: {{{literal}}}}}) "
                    f"{{ settings {{ autoDownloadNewChapters }} }} }}")
    data = await gql(http, "mutation { fetchExtensions(input: {}) { extensions { pkgName isInstalled } } }",
                     timeout=120)
    known = {e["pkgName"]: e["isInstalled"] for e in data["fetchExtensions"]["extensions"]}
    wanted = [PREFIX + p for p in EXTENSIONS]
    missing_from_store = [p for p in wanted if p not in known]
    to_install = [p for p in wanted if p in known and not known[p]]
    if to_install:
        await gql(http, "mutation($ids: [String!]!) { updateExtensions(input: {ids: $ids, patch: {install: true}}) "
                        "{ extensions { pkgName } } }", {"ids": to_install}, timeout=300)
        log(f"Suwayomi : {len(to_install)} source(s) de mangas installée(s)")
    if missing_from_store:
        log("Suwayomi : sources absentes du catalogue (ignorées) : "
            + ", ".join(p.removeprefix(PREFIX) for p in missing_from_store))
    return True


async def status(http):
    data = await gql(http, """query {
        aboutServer { version }
        settings { autoDownloadNewChapters excludeEntryWithUnreadChapters excludeNotStarted }
        sources { nodes { lang } }
        downloadStatus { state queue { state } }
    }""", timeout=20)
    langs = [s["lang"] for s in data["sources"]["nodes"]]
    errors = sum(1 for q in data["downloadStatus"]["queue"] if q["state"] == "ERROR")
    pending = sum(1 for q in data["downloadStatus"]["queue"] if q["state"] in ("QUEUED", "DOWNLOADING"))
    st = data["settings"]
    ok_settings = st["autoDownloadNewChapters"] and not st["excludeEntryWithUnreadChapters"] \
        and not st["excludeNotStarted"]
    return {"version": data["aboutServer"]["version"], "fr": langs.count("fr"), "en": langs.count("en"),
            "errors": errors, "pending": pending, "settings_ok": ok_settings}


# ---------------- Recherche (VF d'abord, puis anglais) ----------------
async def wanted_sources(http):
    data = await gql(http, "query { sources { nodes { id displayName lang extension { pkgName } } } }")
    order = {PREFIX + p: i for i, p in enumerate(EXTENSIONS)}
    srcs = [s for s in data["sources"]["nodes"]
            if s["lang"] in LANGS and (s.get("extension") or {}).get("pkgName") in order]
    srcs.sort(key=lambda s: (LANGS.index(s["lang"]), order[s["extension"]["pkgName"]]))
    return srcs


async def _search_one(http, src, query):
    try:
        data = await gql(http, """mutation($source: LongString!, $q: String) {
            fetchSourceManga(input: {source: $source, type: SEARCH, page: 1, query: $q}) {
                mangas { id title thumbnailUrl inLibrary description status } } }""",
                         {"source": src["id"], "q": query}, timeout=25)
        return src, data["fetchSourceManga"]["mangas"][:8]
    except Exception:
        return src, []   # une source en panne ne bloque pas les autres


async def search(http, query):
    """Résultats regroupés par titre ; chaque groupe liste ses sources, FR d'abord."""
    srcs = await wanted_sources(http)
    if not srcs:
        raise SuwayomiError("aucune source de mangas installée pour l'instant, réessaie dans une minute")
    results = await asyncio.gather(*(_search_one(http, s, query) for s in srcs))
    groups, q = {}, norm(query)
    for prio, (src, mangas) in enumerate(results):
        for rank, m in enumerate(mangas):
            key = norm(m["title"])
            if not key:
                continue
            g = groups.setdefault(key, {"title": m["title"], "candidates": [], "best": (99, 99)})
            g["candidates"].append({**m, "lang": src["lang"], "source": src["displayName"], "prio": prio})
            # pertinence : titre exact > titre qui contient la recherche > ordre de la source
            match = 0 if key == q else (1 if q in key else 2)
            g["best"] = min(g["best"], (match, rank))
    out = []
    for g in groups.values():
        g["candidates"].sort(key=lambda c: c["prio"])          # FR d'abord, puis ordre des sources
        top = g["candidates"][0]
        g.update(lang=top["lang"], source=top["source"], thumbnail=top.get("thumbnailUrl"),
                 description=top.get("description"), in_library=any(c["inLibrary"] for c in g["candidates"]))
        out.append(g)
    out.sort(key=lambda g: (g["best"], LANGS.index(g["lang"])))
    return out[:25]


# ---------------- Ajout à la bibliothèque ----------------
async def add(http, group):
    """Essaie les sources dans l'ordre (VF d'abord) jusqu'à en trouver une avec des chapitres."""
    last_error = None
    for c in group["candidates"]:
        try:
            data = await gql(http, """mutation($id: Int!) {
                fetchMangaAndChapters(input: {id: $id, fetchManga: true, fetchChapters: true}) {
                    manga { id title thumbnailUrl } chapters { id isDownloaded } } }""",
                             {"id": c["id"]}, timeout=120)
        except SuwayomiError as ex:
            last_error = ex
            continue
        chapters = data["fetchMangaAndChapters"]["chapters"]
        if not chapters:
            continue
        manga = data["fetchMangaAndChapters"]["manga"]
        await gql(http, "mutation($id: Int!) { updateManga(input: {id: $id, patch: {inLibrary: true}}) "
                        "{ manga { id } } }", {"id": manga["id"]})
        todo = [ch["id"] for ch in chapters if not ch["isDownloaded"]]
        if todo:
            await gql(http, "mutation($ids: [Int!]!) { enqueueChapterDownloads(input: {ids: $ids}) "
                            "{ downloadStatus { state } } }", {"ids": todo}, timeout=90)
            await gql(http, "mutation { startDownloader(input: {}) { downloadStatus { state } } }")
        thumbs = [manga.get("thumbnailUrl"), c.get("thumbnailUrl")] + [x.get("thumbnailUrl") for x in group["candidates"]]
        return {"id": manga["id"], "title": manga["title"], "lang": c["lang"], "source": c["source"],
                "chapters": len(chapters), "queued": len(todo),
                "thumbnail": next((t for t in thumbs if public_thumbnail(t)), None)}
    if last_error:
        raise last_error
    raise SuwayomiError("aucune source n'a de chapitres disponibles pour ce titre")


# ---------------- Suivi des nouveaux chapitres ----------------
async def library_progress(http):
    """[(id, titre, chapitres téléchargés, langue, source, encore en cours ?)]"""
    data = await gql(http, """query {
        mangas(condition: {inLibrary: true}) { nodes { id title downloadCount source { lang displayName } } }
        downloadStatus { queue { mangaId state } }
    }""", timeout=60)
    busy = {q["mangaId"] for q in data["downloadStatus"]["queue"] if q["state"] in ("QUEUED", "DOWNLOADING")}
    out = []
    for m in data["mangas"]["nodes"]:
        src = m.get("source") or {}
        out.append((m["id"], m["title"], m["downloadCount"], src.get("lang", "?"),
                    src.get("displayName", "?"), m["id"] in busy))
    return out


async def library(http):
    data = await gql(http, "query { mangas(condition: {inLibrary: true}) { nodes { id title } } }")
    return data["mangas"]["nodes"]


async def title_of(http, manga_id):
    data = await gql(http, "query($id: Int!) { manga(id: $id) { title } }", {"id": int(manga_id)})
    return data["manga"]["title"]


def public_thumbnail(url):
    """Discord n'affiche que des images publiques (pas l'adresse interne de Suwayomi)."""
    return url if url and url.startswith("http") and "suwayomi" not in url else None
