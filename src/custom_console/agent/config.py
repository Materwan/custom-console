from pathlib import Path
from dotenv import load_dotenv
from typing import Dict, List
import os

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

PERMISSION_PROMPT = "Accept (Y|n) : "
RENDER_INTERVAL = 0.08  # secondes entre deux rafraîchissements du Live
YES_ANSWERS = ("", "y", "yes", "o", "oui")

# Niveau d'autorisation automatique de l'agent.
#
# Chaque outil déclare, via `ask_permission(level=...)`, le niveau de risque
# minimal requis pour son action. Si le niveau d'autorisation automatique de
# la console (`auto_permission_level`) est supérieur ou égal à ce niveau, la
# permission est accordée automatiquement, sans interrompre l'utilisateur.
#
# Exemple : avec `auto_permission_level=2`, un outil qui ne requiert qu'un
# niveau 1 (ex: lecture) sera auto-autorisé, tout comme un outil qui requiert
# un niveau 2 (ex: envoi d'un email). Avec `auto_permission_level=0` (valeur
# par défaut), rien n'est auto-autorisé : l'utilisateur est toujours consulté.
PERMISSION_LEVEL_NONE = 0  # Aucune auto-autorisation, on demande toujours.
PERMISSION_LEVEL_READ = 1  # Actions peu sensibles (lecture, consultation).
PERMISSION_LEVEL_WRITE = 2  # Actions plus sensibles (écriture, envoi, ...).
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
LOCATION_URL = "https://ipinfo.io/json"
MAX_FORECAST_DAYS = 16

raw_path = os.environ.get(
    "AGENT_LOG_PATH",
    "src/custom_console/agent/data/logs/agent_logs.jsonl",
)
AGENT_LOG_PATH = (PROJECT_ROOT / raw_path).resolve()

# Fichier SQLite unique regroupant l'historique des sessions (Storage) et la
# mémoire utilisateur long terme (Memory) de l'agent. Persiste entre les
# lancements du script.
raw_path = os.environ.get(
    "AGENT_DB_PATH",
    "src/custom_console/agent/data/agent_storage/memory/agent_memory.db",
)
AGENT_DB_PATH = (PROJECT_ROOT / raw_path).resolve()

# Fichier de cache de l'agent
# Sauvegarde
raw_path = os.environ.get(
    "AGENT_CACHE_PATH",
    "src/custom_console/agent/data/agent_storage/cache.json",
)
AGENT_CACHE_PATH = (PROJECT_ROOT / raw_path).resolve()

# La console étant mono-utilisateur, un identifiant fixe suffit à rattacher
# toutes les mémoires à la même personne d'un lancement à l'autre.
AGENT_USER_ID = os.environ.get("AGENT_USER_ID", "default_user")

# Un session_id fixe permet de reprendre l'historique de conversation d'une
# exécution à l'autre. Utiliser une valeur différente (ou None) pour démarrer
# une session vierge sans perdre les sessions précédentes en base.
AGENT_SESSION_ID = os.environ.get("AGENT_SESSION_ID", "console_session")

# Cookie de connexion Moodle
raw_path = os.environ.get(
    "MOODLE_COOKIE_PATH",
    "config/cookies/moodle_cookies.json",
)
MOODLE_COOKIE_PATH = (PROJECT_ROOT / raw_path).resolve()

# Dossiers "workspace" librement accessibles à l'agent pour lire/écrire/
# manipuler des fichiers, sans avoir à demander une permission par chemin
# comme le reste du système de fichiers :
# - "result" : sorties destinées à l'utilisateur (documents produits, exports...).
# - "tmp"    : fichiers de travail temporaires (téléchargements intermédiaires,
#              brouillons, ...), à considérer comme jetable.
# Toute opération de fichier de l'agent sur ces dossiers reste néanmoins
# cantonnée à leur contenu : impossible d'en sortir via "..", un chemin
# absolu ou un lien symbolique (voir `_resolve_workspace_path`).
raw_path = os.environ.get(
    "AGENT_RESULT_DIR",
    "src/custom_console/agent/data/agent_storage/result",
)
AGENT_RESULT_DIR = (PROJECT_ROOT / raw_path).resolve()
raw_path = os.environ.get(
    "AGENT_TMP_DIR",
    "src/custom_console/agent/data/agent_storage/tmp",
)
AGENT_TMP_DIR = (PROJECT_ROOT / raw_path).resolve()
WORKSPACE_ROOTS: Dict[str, str] = {
    "result": AGENT_RESULT_DIR,
    "tmp": AGENT_TMP_DIR,
}

# Niveau d'autorisation automatique, configurable via la variable
# d'environnement AGENT_AUTO_PERMISSION_LEVEL (0 = tout demander,
# 1 = auto-autoriser les actions de lecture, 2 = auto-autoriser aussi
# les actions plus sensibles comme l'envoi d'email).
AUTO_AGENT_PREMISSION = os.environ.get("AUTO_AGENT_PERMISSION", "0")


DEFAULT_WEATHER_VARIABLES: Dict[str, List[str]] = {
    "current": [
        "temperature_2m",
        "apparent_temperature",
        "relative_humidity_2m",
        "wind_speed_10m",
        "weather_code",
        "precipitation",
    ],
    "hourly": [
        "temperature_2m",
        "precipitation_probability",
        "precipitation",
        "wind_speed_10m",
        "weather_code",
    ],
    "daily": [
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_sum",
        "weather_code",
        "wind_speed_10m_max",
    ],
}

# Les instructions par défaut que l'agent doit respecter.
DEFAULT_AGENT_INSTRUCTIONS = instructions = (
    [
        "Tu es un assistant personnel et tu dois m'appeler Monsieur.",
        "Exécute les tâches demandées en utilisant les outils disponibles.",
        "Tous les outils Python fournis renvoient un objet contenant la "
        "réussite de l'appel, puis des informations complémentaires.",
        "Pour Moodle : un cours est toujours identifié par un id "
        "numérique, jamais par son nom. Avant d'appeler "
        "moodle_get_course_structure ou tout autre outil nécessitant un "
        "course_id, appelle d'abord moodle_list_courses pour retrouver "
        "l'id correspondant au nom du cours demandé par l'utilisateur. "
        "N'invente jamais un id et ne le devine pas à partir du HTML "
        "d'une autre page.",
    ],
)
