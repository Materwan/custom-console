"""Module pour la manipulation de fichiers/dossiers (local + reMarkable).

Ce module est volontairement indépendant de toute couche d'affichage
(rich.Console, ToolResult, ...) : il expose une logique métier commune,
utilisée à la fois par la console interactive et par l'agent. Toute erreur
est signalée via des exceptions standard (FileNotFoundError,
NotADirectoryError, PermissionError, ...) ; chaque appelant est responsable
de la mettre en forme pour son propre public (utilisateur humain vs LLM).
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import unicodedata
import time

from typing import Dict, List, Optional, Tuple

from ..config import RMAPI_PATH, REMARKABLE_SYNC_PATH


class InReMarkableError(Exception):
    """Levée quand une opération n'a pas de sens sur reMarkable (ex: cat)."""


class NotAFileError(OSError):
    """Levée quand un chemin devait désigner un fichier mais n'en est pas un."""


class RemarkableUnavailableError(RuntimeError):
    """Levée quand reMarkable est demandé mais RMAPI_PATH n'est pas configuré."""


class IsVirtualRootError(Exception):
    """Levée quand une opération (cat, stat, find, cp...) n'a pas de sens
    car on se trouve sur la racine virtuelle "/" (qui n'est pas un vrai
    dossier, juste un menu de sélection vers C:/, reMarkable/, wsl-Ubuntu/)."""


# Chemin UNC exposé nativement par Windows pour accéder aux fichiers d'une
# distribution WSL2. Fonctionne directement avec les fonctions standards
# (os.chdir, os.listdir, open, ...), pas besoin de backend dédié.
WSL_UBUNTU_PATH = r"\\wsl$\Ubuntu\\"

# Entrées affichées à la racine virtuelle "/".
VIRTUAL_ROOT_STATIC_ENTRIES = ["reMarkable/", "wsl-Ubuntu/"]


class RemarkableBackend:
    """
    Wrapper autour de rmapi.exe en mode "one-shot" (`rmapi.exe <cmd> <args>`),
    le seul mode qui fonctionne correctement hors d'un vrai terminal : le mode
    interactif (`rmapi.exe` seul, avec son prompt `[/]>`) utilise une lib
    readline qui a besoin d'un TTY et ne lit pas les commandes envoyées via
    un pipe stdin (d'où le "EOF" immédiat).

    Comme chaque appel au binaire est indépendant (rmapi ne garde aucun état
    entre deux invocations), c'est nous qui suivons le "dossier distant
    courant" côté Python, et qui le recombinons avec le chemin demandé avant
    chaque commande.
    """

    # Délai minimal (s) entre deux invocations successives de rmapi.exe :
    # chaque appel one-shot recrée un token d'authentification côté cloud
    # reMarkable, et un enchaînement trop rapide déclenche un 429.
    MIN_CALL_INTERVAL = 0.3
    MAX_RETRY_ATTEMPTS = 6
    INITIAL_RETRY_DELAY = 2.0

    def __init__(self, exe_path: str = RMAPI_PATH):
        self.exe_path = exe_path
        self.path = "/"  # dossier distant courant, à la manière d'un chemin posix
        self._last_call_at = 0.0

    def _run(self, *args: str, cwd: Optional[str] = None) -> str:
        delay = self.INITIAL_RETRY_DELAY
        output = ""
        for attempt in range(1, self.MAX_RETRY_ATTEMPTS + 1):
            wait = self.MIN_CALL_INTERVAL - (time.monotonic() - self._last_call_at)
            if wait > 0:
                time.sleep(wait)

            proc = subprocess.run(
                [self.exe_path, *args],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
            )
            self._last_call_at = time.monotonic()
            output = proc.stdout + proc.stderr

            # 429 = rate limit côté cloud reMarkable (trop de créations de
            # token en peu de temps) : on réessaie avec un backoff
            # exponentiel plutôt que de remonter une erreur immédiatement.
            if "429" in output and attempt < self.MAX_RETRY_ATTEMPTS:
                time.sleep(delay)
                delay *= 2
                continue
            return output
        return output

    def _resolve(self, subpath: str) -> str:
        """Combine le dossier courant et le chemin demandé (gère '.', '..', '/abs')."""
        if subpath in (".", ""):
            base = self.path
        elif subpath.startswith("/"):
            base = subpath
        else:
            base = posixpath.join(self.path, subpath)
        return posixpath.normpath(base) or "/"

    @staticmethod
    def _parse_ls(output: str) -> List[str]:
        """
        Sortie attendue (mode one-shot), une ligne par entrée :
            [f]     NomDuFichier
            [d]     NomDuDossier
        """
        entries: List[str] = []
        for raw_line in output.splitlines():
            # On ne retire QUE les caractères de fin de ligne : certains
            # noms de documents se terminent par un espace, et un simple
            # `.rstrip()` (sans argument) le supprimerait, faisant échouer
            # `get`/`put` ensuite ("file doesn't exist") puisque le nom
            # envoyé à rmapi ne correspondrait plus au nom réel distant.
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                if parts[0] == "[f]":
                    entries.append(parts[1])
                elif parts[0] == "[d]":
                    entries.append(parts[1] + "/")
        return entries

    @staticmethod
    def _parse_ls_typed(output: str) -> List[Tuple[str, bool]]:
        """Comme _parse_ls mais retourne (nom, est_dossier)."""
        entries: List[Tuple[str, bool]] = []
        for raw_line in output.splitlines():
            line = raw_line.rstrip("\r\n")  # cf. commentaire dans _parse_ls
            if not line.strip():
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[0] in ("[f]", "[d]"):
                entries.append((parts[1], parts[0] == "[d]"))
        return entries

    def listdir(self, subpath: str = ".") -> List[str]:
        target = self._resolve(subpath)
        raw_output = self._run("ls", target)
        if "ERROR" in raw_output:
            raise FileNotFoundError(target)
        return self._parse_ls(raw_output)

    def listdir_typed(self, subpath: str = ".") -> List[Tuple[str, bool]]:
        target = self._resolve(subpath)
        return self._parse_ls_typed(self._run("ls", target))

    def cd(self, subpath: str) -> None:
        target = self._resolve(subpath)
        raw_output = self._run("ls", target)
        if "ERROR" in raw_output:
            raise FileNotFoundError(target)

        # rmapi liste aussi bien un dossier (son contenu) qu'un fichier (lui-
        # même). Si la seule entrée renvoyée correspond exactement au nom du
        # chemin demandé, c'est que la cible est un fichier, pas un dossier.
        entries = self._parse_ls(raw_output)
        basename = posixpath.basename(target)
        if len(entries) == 1 and entries[0].rstrip("/") == basename:
            raise NotADirectoryError(target)

        self.path = target

    def get(self, filename: str, dest: str = ".") -> None:
        target = self._resolve(filename)
        # rmapi ne prend pas de dossier de destination en argument : `get`
        # télécharge toujours dans le cwd du processus. On change donc le
        # cwd du sous-processus plutôt que de passer `dest` en argument
        # (sinon `dest` est silencieusement ignoré par rmapi, et le fichier
        # atterrit dans le cwd de *ce* processus Python).
        os.makedirs(dest, exist_ok=True)
        output = self._run("get", target, cwd=dest)
        if "ERROR" in output:
            raise FileNotFoundError(target)

    def put(self, local_path: str, remote_path: str = ".") -> None:
        """Envoie un fichier local vers la tablette."""
        target = self._resolve(remote_path)
        output = self._run("put", local_path, target)
        if "ERROR" in output:
            raise FileNotFoundError(target)

    def stat_entry(self, name: str) -> Optional[str]:
        """rmapi n'a pas de vrai `stat` ; on se contente de dire si
        l'entrée existe dans le dossier courant (fichier ou dossier)."""
        entries = self.listdir(".")
        return name if name in entries else None

    def is_dir(self, subpath: str) -> bool:
        """Indique si `subpath` désigne un dossier distant (par opposition à
        un fichier). Lève FileNotFoundError si l'entrée n'existe pas."""
        target = self._resolve(subpath)
        if target == "/":
            return True
        parent = posixpath.dirname(target) or "/"
        basename = posixpath.basename(target)
        for name, is_dir in self.listdir_typed(parent):
            if name == basename:
                return is_dir
        raise FileNotFoundError(target)

    def pwd(self) -> str:
        return self.path

    def sync(self, dest: str = REMARKABLE_SYNC_PATH) -> None:
        """Copie récursivement l'ensemble des fichiers et dossiers de la
        tablette (depuis la racine distante "/") vers le dossier local
        `dest`, en reconstituant l'arborescence.

        Ne modifie pas `self.path` (le "dossier distant courant" n'est pas
        affecté par l'opération) : les chemins distants sont résolus de
        façon absolue tout au long de la synchronisation.
        """
        os.makedirs(dest, exist_ok=True)
        self._sync_dir("/", dest)

    def _sync_dir(self, remote_path: str, local_path: str) -> None:
        for name, is_dir in self.listdir_typed(remote_path):
            remote_entry = posixpath.join(remote_path, name)
            local_entry = os.path.join(local_path, name)
            if is_dir:
                os.makedirs(local_entry, exist_ok=True)
                self._sync_dir(remote_entry, local_path=local_entry)
            else:
                try:
                    print(f"Dowloading: '{remote_entry}'")
                    time.sleep(0.1)
                    self.get(remote_entry, dest=local_path)
                except FileNotFoundError as e:
                    print(f"Skipped: {e}.")


class FileManager:
    """Gère la navigation et les opérations fichiers, local + reMarkable."""

    # Modes possibles pour `self.mode`
    MODE_ROOT = "root"
    MODE_LOCAL = "local"
    MODE_REMARKABLE = "remarkable"

    def __init__(self) -> None:
        self.local_location: str = os.getcwd().replace("\\", "/")
        self.mode: str = self.MODE_LOCAL

        self.remarkable: Optional[RemarkableBackend] = (
            RemarkableBackend() if RMAPI_PATH and os.path.isfile(RMAPI_PATH) else None
        )

    # -- Etat / compatibilité ascendante -------------------------------------- #

    @property
    def in_remarkable(self) -> bool:
        return self.mode == self.MODE_REMARKABLE

    @in_remarkable.setter
    def in_remarkable(self, value: bool) -> None:
        # Conservé pour compatibilité avec du code externe qui écrirait
        # encore `file_manager.in_remarkable = True/False`.
        self.mode = self.MODE_REMARKABLE if value else self.MODE_LOCAL

    @property
    def in_root(self) -> bool:
        return self.mode == self.MODE_ROOT

    @property
    def location(self) -> str:
        if self.mode == self.MODE_ROOT:
            return "/"
        if self.mode == self.MODE_REMARKABLE:
            assert self.remarkable is not None
            return f"reMarkable:{self.remarkable.pwd()}"
        return self.local_location

    def get_working_directory(self) -> str:
        return self.location

    # -- Helpers --------------------------------------------------------------- #

    @staticmethod
    def _strip_remote_prefix(path: str) -> Optional[str]:
        lowered = path.lower()
        for prefix in ("remarkable:", "/remarkable/", "/remarkable"):
            if lowered.startswith(prefix):
                remainder = path[len(prefix) :]
                return remainder if remainder else "."
        return None

    def _expand_path(self, path: str) -> str:
        if self.mode != self.MODE_LOCAL:
            return path.replace("\\", "/")
        return os.path.expanduser(path).replace("\\", "/")

    def _require_remarkable(self) -> RemarkableBackend:
        if self.remarkable is None:
            raise RemarkableUnavailableError(
                "reMarkable n'est pas configuré (RMAPI_PATH manquant ou invalide)"
            )
        return self.remarkable

    def _require_not_root(self, action: str) -> None:
        if self.mode == self.MODE_ROOT:
            raise IsVirtualRootError(
                f"'{action}' n'a pas de sens sur la racine virtuelle '/'. "
                "Déplace-toi d'abord vers C:/, reMarkable/ ou wsl-Ubuntu/."
            )

    @staticmethod
    def _list_available_drives() -> List[str]:
        """Liste dynamiquement les lettres de lecteurs Windows montés."""
        import string

        drives = []
        for letter in string.ascii_uppercase:
            if os.path.exists(f"{letter}:/"):
                drives.append(f"{letter}:/")
        return drives

    def _virtual_root_entries(self) -> List[str]:
        return self._list_available_drives() + VIRTUAL_ROOT_STATIC_ENTRIES

    def virtual_root_entries(self) -> List[str]:
        """Version publique de `_virtual_root_entries`, utilisable depuis
        l'extérieur (ex: la complétion) indépendamment du mode courant."""
        return self._virtual_root_entries()

    # -- Navigation ---------------------------------------------------------- #

    def change_directory(self, path: str) -> None:
        raw = path.strip()

        # Cas 0 : "/" ramène (ou reste) sur la racine virtuelle, quel que
        # soit l'endroit d'où on part (local, reMarkable, ou déjà root).
        if raw in ("/", "\\"):
            self.mode = self.MODE_ROOT
            return

        # Cas 0bis : un chemin virtuel ABSOLU ("/reMarkable/...",
        # "/wsl-Ubuntu/...", "/C:/...") doit fonctionner depuis n'importe quel
        # dossier courant, pas seulement depuis la racine virtuelle "/".
        if raw.startswith("/") or raw.startswith("\\"):
            stripped = raw.strip("/\\")
            first_segment = (
                re.split(r"[/\\]", stripped, maxsplit=1)[0] if stripped else ""
            )
            first_lower = first_segment.lower()
            if first_lower in ("remarkable", "wsl-ubuntu") or re.fullmatch(
                r"[a-zA-Z]:", first_segment
            ):
                self._change_directory_from_root(raw)
                return

        # Cas 1 : "~" ou "~/..." ramène toujours au système de fichiers
        # local (utile même depuis la racine virtuelle ou reMarkable).
        if raw == "~" or raw.startswith("~/") or raw.startswith("~\\"):
            home_target = os.path.expanduser(raw).replace("\\", "/")
            if os.path.isfile(home_target):
                raise NotADirectoryError(home_target)
            if not os.path.isdir(home_target):
                raise FileNotFoundError(home_target)
            os.chdir(home_target)
            self.mode = self.MODE_LOCAL
            self.local_location = os.getcwd().replace("\\", "/")
            return

        # Cas 2 : on est sur la racine virtuelle -> router vers le bon
        # système de fichiers en fonction du premier segment du chemin.
        if self.mode == self.MODE_ROOT:
            self._change_directory_from_root(raw)
            return

        target = self._expand_path(path)

        # Cas 3 : on est en local et on entre dans reMarkable.
        if self.mode == self.MODE_LOCAL:
            remote_target = self._strip_remote_prefix(target)
            if remote_target is not None:
                remarkable = self._require_remarkable()
                self.mode = self.MODE_REMARKABLE
                remarkable.path = "/"
                if remote_target not in (".", ""):
                    try:
                        remarkable.cd(remote_target)
                    except Exception:
                        self.mode = self.MODE_LOCAL
                        raise
                return

        # Cas 4 : on est déjà sur la tablette.
        if self.mode == self.MODE_REMARKABLE:
            remarkable = self._require_remarkable()
            remarkable.cd(target)
            return

        # Cas 5 : navigation locale classique (fonctionne aussi bien pour
        # C:/... que pour \\wsl$\Ubuntu\..., ce sont juste des chemins
        # locaux du point de vue de Windows/Python).
        if os.path.isfile(target):
            raise NotADirectoryError(target)
        if not os.path.isdir(target):
            raise FileNotFoundError(target)
        os.chdir(target)
        self.local_location = os.getcwd().replace("\\", "/")

    def _change_directory_from_root(self, raw: str) -> None:
        """Route un `cd` fait depuis la racine virtuelle "/" vers le bon
        système de fichiers, en fonction du premier segment du chemin."""
        first, _, rest = raw.strip("/\\").partition("/")
        first_lower = first.lower()

        # -> reMarkable/...
        if first_lower == "remarkable":
            remarkable = self._require_remarkable()
            self.mode = self.MODE_REMARKABLE
            remarkable.path = "/"
            if rest:
                try:
                    remarkable.cd(rest)
                except Exception:
                    self.mode = self.MODE_ROOT
                    raise
            return

        # -> wsl-Ubuntu/...
        if first_lower == "wsl-ubuntu":
            target = WSL_UBUNTU_PATH
            if rest:
                target = os.path.join(WSL_UBUNTU_PATH, rest)
            target = target.replace("\\", "/")
            if os.path.isfile(target):
                raise NotADirectoryError(target)
            if not os.path.isdir(target):
                raise FileNotFoundError(target)
            os.chdir(target)
            self.mode = self.MODE_LOCAL
            self.local_location = os.getcwd().replace("\\", "/")
            return

        # -> C:/... (ou n'importe quelle lettre de lecteur détectée)
        if re.fullmatch(r"[a-zA-Z]:", first):
            drive = first.upper() + "/"
            target = drive if not rest else posixpath.join(drive, rest)
            if os.path.isfile(target):
                raise NotADirectoryError(target)
            if not os.path.isdir(target):
                raise FileNotFoundError(target)
            os.chdir(target)
            self.mode = self.MODE_LOCAL
            self.local_location = os.getcwd().replace("\\", "/")
            return

        raise FileNotFoundError(raw)

    # -- Copie ---------------------------------------------------------------- #

    def copy(self, src: str, dst: str, recursive: bool) -> None:
        self._require_not_root("cp")

        if self.mode == self.MODE_REMARKABLE:
            remarkable = self._require_remarkable()
            if remarkable.is_dir(src):
                raise ValueError(
                    "Copie de dossier à dossier au sein de la tablette non "
                    "supportée (rmapi ne sait ni télécharger, ni envoyer un "
                    "dossier entier) ; utilisez `sync` pour exporter un "
                    "dossier reMarkable en local."
                )
            tmp_dir = tempfile.mkdtemp(prefix="rmapi_cp_")
            try:
                remarkable.get(src, tmp_dir)
                downloaded = os.listdir(tmp_dir)
                if not downloaded:
                    raise FileNotFoundError(src)
                local_tmp_file = os.path.join(tmp_dir, downloaded[0])
                remarkable.put(local_tmp_file, dst)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        remote_dst = self._strip_remote_prefix(dst)
        if remote_dst is not None:
            remarkable = self._require_remarkable()
            local_src = self._expand_path(src)
            remarkable.put(local_src, remote_dst)
            return

        remote_src = self._strip_remote_prefix(src)
        if remote_src is not None:
            remarkable = self._require_remarkable()
            local_dst = self._expand_path(dst)
            if remarkable.is_dir(remote_src):
                if not recursive:
                    raise ValueError(f"-r not specified; omitting directory '{src}'")
                remote_target = remarkable._resolve(remote_src)
                # Si `local_dst` est un dossier existant, on y crée un
                # sous-dossier portant le nom de la source (comme `cp -r`) ;
                # sinon `local_dst` désigne directement le dossier cible.
                if os.path.isdir(local_dst):
                    dir_name = (
                        posixpath.basename(remote_target.rstrip("/")) or "reMarkable"
                    )
                    local_dst = os.path.join(local_dst, dir_name)
                os.makedirs(local_dst, exist_ok=True)
                remarkable._sync_dir(remote_target, local_dst)
            else:
                remarkable.get(remote_src, local_dst)
            return

        src = self._expand_path(src)
        dst = self._expand_path(dst)
        if os.path.isdir(src):
            if not recursive:
                raise ValueError(f"-r not specified; omitting directory '{src}'")
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif os.path.isfile(src):
            shutil.copy2(src, dst)
        else:
            raise FileNotFoundError(src)

    # -- Stat ------------------------------------------------------------------ #

    def stat(self, path: str) -> List[bool]:
        self._require_not_root("stat")

        if self.mode == self.MODE_REMARKABLE:
            remarkable = self._require_remarkable()
            found = remarkable.stat_entry(path)
            if not found:
                raise FileNotFoundError(path)
            return [True, True, True]

        file_expanded = self._expand_path(path)
        if not (os.path.isfile(file_expanded) or os.path.isdir(file_expanded)):
            raise FileNotFoundError(file_expanded)

        return [
            os.access(file_expanded, os.R_OK),
            os.access(file_expanded, os.W_OK),
            os.access(file_expanded, os.X_OK),
        ]

    # -- Find ------------------------------------------------------------------ #

    def _find(
        self, pattern: str, path: str, depth: int, dir_only: bool, strict: bool
    ) -> List[str]:
        # (inchangé)
        if depth == 0:
            return []

        res: List[str] = []
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)

        try:
            for entry in os.listdir(path):
                full_path = os.path.join(path, entry)
                is_match = bool(
                    regex.fullmatch(entry) if strict else regex.search(entry)
                )

                if os.path.isdir(full_path):
                    if dir_only:
                        if is_match:
                            res.append(full_path)
                    else:
                        if is_match:
                            res.append(full_path)
                        res.extend(
                            self._find(
                                pattern=pattern,
                                path=full_path,
                                depth=depth - 1,
                                dir_only=dir_only,
                                strict=strict,
                            )
                        )
                elif not dir_only and is_match:
                    res.append(full_path)
        except PermissionError:
            pass

        return res

    def find(self, pattern: str, path: str, depth: int, strict: bool) -> List[str]:
        self._require_not_root("find")

        if not pattern:
            raise ValueError("find: pattern must not be empty.")

        is_dir_search = pattern[-1] in "/\\"
        search_pattern = pattern[:-1] if is_dir_search else pattern

        if os.path.isfile(path):
            raise NotADirectoryError(path)
        if not os.path.isdir(path):
            raise FileNotFoundError(path)

        res = self._find(
            pattern=search_pattern,
            path=path,
            depth=depth,
            dir_only=is_dir_search,
            strict=strict,
        )
        return sorted(res)

    # -- Cat ------------------------------------------------------------------- #

    def cat(self, path: str) -> str:
        self._require_not_root("cat")

        if self.mode == self.MODE_REMARKABLE:
            raise InReMarkableError(
                "Impossible d'afficher un fichier reMarkable directement "
                "(ce sont des notebooks, pas du texte)."
            )

        file_expanded = self._expand_path(path)
        if os.path.isdir(file_expanded):
            raise NotAFileError(file_expanded)
        if not os.path.isfile(file_expanded):
            raise FileNotFoundError(file_expanded)

        with open(file_expanded, encoding="utf-8") as f:
            return f.read()

    # -- List ------------------------------------------------------------------ #

    def list(self, path: str, all: bool) -> List[str]:
        # Sur la racine virtuelle, `ls` (sans argument, ou avec "." / "/")
        # affiche le menu des systèmes de fichiers disponibles plutôt que
        # de lever une erreur : contrairement aux autres commandes, "ls /"
        # a un sens ici.
        if self.mode == self.MODE_ROOT:
            if path not in (".", "/", "\\", ""):
                raise IsVirtualRootError(
                    f"'{path}' n'existe pas sous la racine virtuelle '/'."
                )
            entries = self._virtual_root_entries()
            return [f"'{e}'" if " " in e else e for e in entries]

        if self.mode == self.MODE_REMARKABLE:
            remarkable = self._require_remarkable()
            entries = remarkable.listdir(path)
            if not all:
                entries = [e for e in entries if not e.startswith(".")]
            return [f"'{e}'" if " " in e else e for e in entries]

        folder_expanded = self._expand_path(path)
        if os.path.isfile(folder_expanded):
            raise NotADirectoryError(folder_expanded)
        if not os.path.isdir(folder_expanded):
            raise FileNotFoundError(folder_expanded)

        if all:
            entries = os.listdir(folder_expanded)
        else:
            entries = [d for d in os.listdir(folder_expanded) if not d.startswith(".")]

        entries = [
            e + "/" if os.path.isdir(os.path.join(folder_expanded, e)) else e
            for e in entries
        ]
        return [f"'{e}'" if " " in e else e for e in entries]
