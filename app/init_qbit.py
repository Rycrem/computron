"""
Initialisation automatique (s'exécute une fois avant qBittorrent/Radarr/Sonarr) :
  - crée les dossiers Films / Séries / Téléchargements s'ils manquent
  - prépare la configuration de qBittorrent :
      * dossier de téléchargement = /data/Téléchargements
      * accès sans mot de passe depuis les autres conteneurs (réseau Docker)
      * mot de passe admin généré et écrit dans docker/qbittorrent/MOT_DE_PASSE_QBITTORRENT.txt
Idempotent : on peut le relancer sans rien casser.
"""
import base64
import configparser
import hashlib
import os
import secrets
import string
import unicodedata

PUID = int(os.getenv("PUID", "1000"))
PGID = int(os.getenv("PGID", "10"))
DATA = "/data"
QB_DIR = "/cfg/qbittorrent"
QB_CONF_DIR = f"{QB_DIR}/qBittorrent"
QB_CONF = f"{QB_CONF_DIR}/qBittorrent.conf"
QB_PASS_FILE = f"{QB_DIR}/MOT_DE_PASSE_QBITTORRENT.txt"


def norm(s):
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if not unicodedata.combining(c))


CREATED = []


def ensure_dir(wanted, aliases):
    """Réutilise un dossier existant (Séries / Series / séries…) ou le crée."""
    for name in os.listdir(DATA):
        if os.path.isdir(os.path.join(DATA, name)) and norm(name) in aliases:
            print(f"  dossier existant : {name}")
            return name
    path = os.path.join(DATA, wanted)
    os.makedirs(path, exist_ok=True)
    os.chown(path, PUID, PGID)
    print(f"  dossier créé : {wanted}")
    CREATED.append(wanted)
    return wanted


def chown_tree(path):
    for root, dirs, files in os.walk(path):
        for n in [root] + [os.path.join(root, x) for x in dirs + files]:
            try:
                os.chown(n, PUID, PGID)
            except OSError:
                pass


def pbkdf2(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
    return f'"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(dk).decode()})"'


def main():
    print("== Initialisation ==")
    ensure_dir("Films", {"films"})
    ensure_dir("Séries", {"series"})
    ensure_dir("Anims", {"anims", "anime", "animes"})
    dl = ensure_dir("Téléchargements", {"telechargements", "downloads"})
    media = os.getenv("MEDIA_DIR", "?")
    print(f"  dossier des médias sur le NAS : {media}")
    if len(CREATED) == 4:
        # Docker crée en silence un dossier vide quand MEDIA_DIR ne pointe pas sur un dossier existant
        print(f"  ⚠️ aucun dossier Films / Séries / Anims / Téléchargements trouvé dans {media} : "
              f"si tes médias sont ailleurs, corrige MEDIA_DIR dans .env puis redéploie le projet")

    # Mangas : dossier MANGA_DIR (créé si besoin) + dossier de Suwayomi accessible en écriture
    if os.path.isdir("/comics"):
        st = os.stat("/comics")
        if st.st_uid == 0:   # dossier tout juste créé par Docker : on le donne à l'utilisateur du NAS
            os.chown("/comics", PUID, PGID)
            os.chmod("/comics", 0o775)
            print("  dossier des mangas préparé")
    os.makedirs("/cfg/suwayomi", exist_ok=True)
    chown_tree("/cfg/suwayomi")

    os.makedirs(QB_CONF_DIR, exist_ok=True)
    conf = configparser.RawConfigParser(strict=False)
    conf.optionxform = str
    if os.path.exists(QB_CONF):
        conf.read(QB_CONF, encoding="utf-8")
    for sec in ("BitTorrent", "Preferences", "LegalNotice"):
        if not conf.has_section(sec):
            conf.add_section(sec)

    save_path = f"/data/{dl}"
    conf.set("LegalNotice", "Accepted", "true")
    conf.set("BitTorrent", "Session\\DefaultSavePath", save_path)
    conf.set("Preferences", "Downloads\\SavePath", save_path)
    conf.set("Preferences", "WebUI\\Port", "8080")
    conf.set("Preferences", "WebUI\\Username", "admin")
    # Les conteneurs (réseau Docker 172.16.0.0/12) accèdent à qBittorrent sans mot de passe
    conf.set("Preferences", "WebUI\\AuthSubnetWhitelistEnabled", "true")
    conf.set("Preferences", "WebUI\\AuthSubnetWhitelist", "172.16.0.0/12")
    conf.set("Preferences", "WebUI\\HostHeaderValidation", "false")

    if not conf.has_option("Preferences", "WebUI\\Password_PBKDF2") or not os.path.exists(QB_PASS_FILE):
        alphabet = string.ascii_letters + string.digits
        password = os.getenv("QBIT_PASS") or "".join(secrets.choice(alphabet) for _ in range(16))
        conf.set("Preferences", "WebUI\\Password_PBKDF2", pbkdf2(password))
        with open(QB_PASS_FILE, "w", encoding="utf-8") as f:
            f.write(f"Interface qBittorrent (réseau local) : http://IP_DU_NAS:8080\n"
                    f"Utilisateur : admin\nMot de passe : {password}\n")
        print("  mot de passe qBittorrent généré (voir MOT_DE_PASSE_QBITTORRENT.txt)")

    with open(QB_CONF, "w", encoding="utf-8") as f:
        conf.write(f, space_around_delimiters=False)
    chown_tree(QB_DIR)
    print("  qBittorrent préconfiguré")
    print("== Initialisation terminée ==")


if __name__ == "__main__":
    main()
