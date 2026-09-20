import sys
import requests

from typing import List, Dict, Any

from custom_console.config import *


def get_installed_models(
    *, size: bool = False, capabilities: bool = False
) -> List[Dict[str, Any]]:
    """Récupère la liste des modèles installés via l'API /api/tags."""
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=1)
        response.raise_for_status()
        data = response.json()
        return [
            {
                "name": model["model"],
                "size": model["size"] if size else None,
                "capabilities": model["capabilities"] if capabilities else None,
            }
            for model in data.get("models", [])
        ]
    except requests.exceptions.RequestException as e:
        return None


def get_running_models(
    *, size: bool = False, capabilities: bool = False
) -> List[Dict[str, Any]]:
    try:
        installed_models = get_installed_models(size=size, capabilities=capabilities)

        response = requests.get(f"{OLLAMA_BASE_URL}/api/ps", timeout=1)
        response.raise_for_status()
        data = response.json()
        names = [model["name"] for model in data.get("models", [])]
        return [model for model in installed_models if model["name"] in names]
    except requests.exceptions.RequestException as e:
        return None


def _search_model(model_name: str, model_list: List[Dict[str, Any]]) -> str:

    model_base, *version = model_name.split(":")
    if version:
        for m in model_list:
            if m["name"] == model_name:
                return m["name"]

    else:
        for m in model_list:
            m_ = m["name"].split(":")[0]
            if m_ == model_base:
                return m["name"]


def get_model(model_name: str) -> bool:
    """Vérifie si le modèle est dans la liste des modèles installés."""
    installed = get_installed_models()

    return _search_model(model_name, installed)


def is_running(model_name: str) -> bool:
    running = get_running_models()

    return bool(_search_model(model_name, running))


if __name__ == "__main__":

    print(get_running_models())

    print(get_model("qwen3.5"))
