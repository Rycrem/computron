"""
Mise à jour automatique du code au démarrage d'un conteneur (arr-bot et init).

  - demande à GitHub le dernier commit de la branche suivie (CODE_REPO / CODE_BRANCH)
  - s'il a changé, télécharge l'archive, vérifie que le code se compile,
    puis remplace /code/app d'un coup (jamais de code à moitié copié)
  - sans Internet ou en cas d'erreur : garde la version déjà installée,
    ou à défaut celle livrée avec le projet (/livre). Le démarrage n'est jamais bloqué.

Désactivable avec MAJ_AUTO=non dans le .env.
"""
import fcntl
import io
import json
import os
import shutil
import tarfile
import urllib.request

REPO = os.getenv("CODE_REPO", "").strip() or "Rycrem/computron"
BRANCH = os.getenv("CODE_BRANCH", "").strip() or "main"
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
AUTO = os.getenv("MAJ_AUTO", "oui").strip().lower() not in {"non", "no", "false", "0", "off"}

LIVRE = "/livre"          # code livré avec le projet (dossier app/ du docker-compose)
CODE = "/code"
DEST = f"{CODE}/app"
NEW = f"{CODE}/app.new"
OLD = f"{CODE}/app.old"
VERSION = f"{CODE}/VERSION"
LOCK = f"{CODE}/.maj.lock"   # arr-bot et init démarrent en même temps sur le même dossier


def log(msg):
    print(f"[maj] {msg}", flush=True)


def get(url, accept):
    headers = {"Accept": accept, "User-Agent": "computron-maj"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        return r.read()


def current_version():
    try:
        with open(VERSION, encoding="utf-8") as f:
            return json.load(f).get("sha")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def extract(archive, target):
    """Ne garde que le dossier app/ de l'archive GitHub (racine « <repo>-<sha>/ »)."""
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/")
            if len(parts) < 3 or parts[1] != "app" or ".." in parts or not (m.isfile() or m.isdir()):
                continue
            path = os.path.join(target, *parts[2:])
            if m.isdir():
                os.makedirs(path, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with tar.extractfile(m) as src, open(path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


def check(target):
    """Refuse une version incomplète ou qui ne se compile pas."""
    for needed in ("bot.py", "autoconfig.py", "manga.py", "init_qbit.py"):
        if not os.path.isfile(os.path.join(target, needed)):
            raise RuntimeError(f"{needed} absent de la nouvelle version")
    for name in os.listdir(target):
        if name.endswith(".py"):
            path = os.path.join(target, name)
            with open(path, encoding="utf-8") as f:
                compile(f.read(), path, "exec")


def install(source_dir):
    if os.path.isdir(DEST):
        shutil.rmtree(OLD, ignore_errors=True)
        os.rename(DEST, OLD)
    os.rename(source_dir, DEST)
    shutil.rmtree(OLD, ignore_errors=True)


def fallback():
    """Aucune version installée : on part de celle livrée avec le projet."""
    if os.path.isfile(f"{DEST}/bot.py"):
        return
    shutil.rmtree(NEW, ignore_errors=True)
    shutil.copytree(LIVRE, NEW, ignore=shutil.ignore_patterns("__pycache__"))
    install(NEW)
    with open(VERSION, "w", encoding="utf-8") as f:
        json.dump({"sha": None, "source": "livrée avec le projet"}, f)
    log("version livrée avec le projet installée")


def update():
    sha = get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
              "application/vnd.github.sha").decode().strip()
    if sha == current_version() and os.path.isfile(f"{DEST}/bot.py"):
        log(f"déjà à jour ({REPO}@{BRANCH}, {sha[:7]})")
        return
    archive = get(f"https://api.github.com/repos/{REPO}/tarball/{sha}", "application/vnd.github+json")
    n = extract(archive, NEW)
    check(NEW)
    install(NEW)
    with open(VERSION, "w", encoding="utf-8") as f:
        json.dump({"sha": sha, "repo": REPO, "branch": BRANCH}, f)
    log(f"code mis à jour vers {sha[:7]} ({n} fichiers, {REPO}@{BRANCH})")


def main():
    os.makedirs(CODE, exist_ok=True)
    with open(LOCK, "w") as lock:
        # un seul conteneur à la fois : le second attend puis voit « déjà à jour »
        fcntl.flock(lock, fcntl.LOCK_EX)
        run()


def run():
    if AUTO:
        try:
            update()
        except Exception as ex:
            shutil.rmtree(NEW, ignore_errors=True)
            log(f"mise à jour impossible, version actuelle conservée ({ex})")
    else:
        log("mise à jour automatique désactivée (MAJ_AUTO=non)")
    fallback()


if __name__ == "__main__":
    main()
