from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PROJECT_ROOT = Path(__file__).resolve().parents[2]

raw_path = os.environ.get("COMMANDS_JSON_PATH", "config/commands.json")
COMMANDS_PATH = (PROJECT_ROOT / raw_path).resolve()

raw_path = os.environ.get("SAVED_APPS_PATH", "src/custom_console/data/saved_app.json")
SAVED_APPS_PATH = (PROJECT_ROOT / raw_path).resolve()

raw_path = os.environ.get(
    "RMAPI_PATH",
)
if not raw_path:
    RMAPI_PATH = None
else:
    RMAPI_PATH = (PROJECT_ROOT / raw_path).resolve()
