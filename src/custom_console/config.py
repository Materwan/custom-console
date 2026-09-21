import os

from typing import Dict, List

from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _path(env_var: str, default: Path | str) -> Path:
    return Path(os.environ.get(env_var, default)).expanduser().resolve()


# --------------------------------------------------------------------------- #
# Config projet (défauts relatifs, surchageables)
# --------------------------------------------------------------------------- #
COMMANDS_JSON_PATH = _path(
    "COMMANDS_JSON_PATH", PROJECT_ROOT / "config" / "commands.json"
)
SAVED_APP_PATH = _path(
    "SAVED_APP_PATH",
    PROJECT_ROOT / "src" / "custom_console" / "data" / "saved_app.json",
)

REMARKABLE_SYNC_PATH = _path(
    "REMARKABLE_SYNC_PATH",
    PROJECT_ROOT / "src" / "custom_console" / "data" / "reMarkable_sync",
)

# Fichier SQLite unique regroupant l'historique des sessions (Storage) et la
# mémoire utilisateur long terme (Memory) de l'agent. Persiste entre les
# lancements du script.
AGENT_DB_PATH = _path(
    "AGENT_DB_PATH",
    PROJECT_ROOT
    / "src"
    / "custom_console"
    / "data"
    / "agent_storage"
    / "memory"
    / "agent_memory.db",
)

AGENT_LOG_PATH = _path(
    "AGENT_LOG_PATH",
    PROJECT_ROOT / "src" / "custom_console" / "data" / "logs" / "agent_logs.jsonl",
)


# Fichier de cache de l'agent
# Sauvegarde
AGENT_CACHE_PATH = _path(
    "AGENT_CACHE_PATH",
    PROJECT_ROOT / "src" / "custom_console" / "data" / "agent_storage" / "cache.json",
)

# Dossiers "workspace" librement accessibles à l'agent pour lire/écrire/
# manipuler des fichiers, sans avoir à demander une permission par chemin
AGENT_RESULT_DIR = _path(
    "AGENT_RESULT_DIR",
    PROJECT_ROOT / "src" / "custom_console" / "data" / "agent_storage" / "result",
)
AGENT_TMP_DIR = _path(
    "AGENT_TMP_DIR",
    PROJECT_ROOT / "src" / "custom_console" / "data" / "agent_storage" / "tmp",
)
WORKSPACE_ROOTS: Dict[str, str] = {
    "result": AGENT_RESULT_DIR,
    "tmp": AGENT_TMP_DIR,
}

# --------------------------------------------------------------------------- #
# Config machine/utilisateur (pas de défaut portable, doit venir du .env)
# --------------------------------------------------------------------------- #
RMAPI_PATH = os.environ.get("RMAPI_PATH")
MOODLE_COOKIE_PATH = _path("MOODLE_STATE", PROJECT_ROOT / "moodle_state.json")


# --------------------------------------------------------------------------- #
# Variables configurables
# --------------------------------------------------------------------------- #

# La console étant mono-utilisateur, un identifiant fixe suffit à rattacher
# toutes les mémoires à la même personne d'un lancement à l'autre.
AGENT_USER_ID = os.environ.get("AGENT_USER_ID", "default_user")

# Un session_id fixe permet de reprendre l'historique de conversation d'une exécution à l'autre.
AGENT_SESSION_ID = os.environ.get("AGENT_SESSION_ID", "console_session")


# Niveau d'autorisation automatique, configurable via la variable
AUTO_AGENT_PREMISSION = os.environ.get("AUTO_AGENT_PERMISSION", "0")


# --------------------------------------------------------------------------- #
# Variables par défault
# --------------------------------------------------------------------------- #
CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PERMISSION_PROMPT = "Accept (Y|n) : "
RENDER_INTERVAL = 0.08  # secondes entre deux rafraîchissements du Live
YES_ANSWERS = ("", "y", "yes", "o", "oui")

# Niveau d'autorisation automatique de l'agent.
# Tout outils nécessitant une autorisation plus faible ou égale,
# sera automatiquement accepté, sans demander à l'utilisateur.
PERMISSION_LEVEL_NONE = 0  # Aucune auto-autorisation, on demande toujours.
PERMISSION_LEVEL_READ = 1  # Actions peu sensibles (lecture, consultation).
PERMISSION_LEVEL_WRITE = 2  # Actions plus sensibles (écriture, envoi, ...).
OLLAMA_BASE_URL = "http://localhost:11434"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
LOCATION_URL = "https://ipinfo.io/json"
MAX_FORECAST_DAYS = 16


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


def check_required_paths() -> None:
    """Valide les chemins critiques au démarrage plutôt qu'à l'usage.

    Trois catégories :
    - required   : fichiers indispensables au fonctionnement, absence fatale.
    - auto_create: dossiers/fichiers de données créés automatiquement si absents.
    - optional   : fonctionnalités dégradées mais non bloquantes si absents.
    """
    missing: list[str] = []
    auto_create: list[str] = []
    optional: list[str] = []

    # Fichiers requis pour démarrer
    for path in [COMMANDS_JSON_PATH]:
        if not Path(path).is_file():
            missing.append(f"{str(path)!r} est introuvable.")

    # Fichiers auto-créés au besoin (par le code qui les écrit)
    for path in [SAVED_APP_PATH, AGENT_LOG_PATH, AGENT_CACHE_PATH]:
        if not Path(path).is_file():
            auto_create.append(f"{str(path)!r} absent, sera créé automatiquement.")

    # Dossiers auto-créés au besoin
    for path in [AGENT_RESULT_DIR, AGENT_TMP_DIR]:
        if not Path(path).is_dir():
            auto_create.append(f"{str(path)!r} absent, sera créé automatiquement.")

    # Chemins optionnels (fonctionnalité dégradée si absents), certains
    # peuvent être None (non configurés dans le .env)
    for path in [AGENT_DB_PATH, RMAPI_PATH, MOODLE_COOKIE_PATH]:
        if path is None or not Path(path).is_file():
            optional.append(f"{str(path)!r} absent (optionnel).")

    if missing:
        raise RuntimeError("Config invalide :\n" + "\n".join(missing))
    if auto_create:
        print("WARN :\n" + "\n".join(auto_create))
    if optional:
        print("INFO :\n" + "\n".join(optional))
