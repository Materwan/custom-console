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

from typing import Dict, List, Optional, Tuple

from custom_console.config import RMAPI_PATH


class InReMarkableError(Exception):
    """Levée quand une opération n'a pas de sens sur reMarkable (ex: cat)."""


class NotAFileError(OSError):
    """Levée quand un chemin devait désigner un fichier mais n'en est pas un."""


class RemarkableUnavailableError(RuntimeError):
    """Levée quand reMarkable est demandé mais RMAPI_PATH n'est pas configuré."""


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

    def __init__(self, exe_path: str = RMAPI_PATH):
        self.exe_path = exe_path
        self.path = "/"  # dossier distant courant, à la manière d'un chemin posix

    def _run(self, *args: str) -> str:
        proc = subprocess.run(
            [self.exe_path, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.stdout + proc.stderr

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
        for line in output.splitlines():
            line = line.rstrip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                if parts[0] == "[f]":
                    entries.append(parts[1].strip())
                elif parts[0] == "[d]":
                    entries.append(parts[1].strip() + "/")
        return entries

    @staticmethod
    def _parse_ls_typed(output: str) -> List[Tuple[str, bool]]:
        """Comme _parse_ls mais retourne (nom, est_dossier)."""
        entries: List[Tuple[str, bool]] = []
        for line in output.splitlines():
            line = line.rstrip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[0] in ("[f]", "[d]"):
                entries.append((parts[1].strip(), parts[0] == "[d]"))
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
        output = self._run("get", target, dest)
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

    def pwd(self) -> str:
        return self.path


class FileManager:
    """Gère la navigation et les opérations fichiers, local + reMarkable."""

    def __init__(self) -> None:
        self.local_location: str = os.getcwd().replace("\\", "/")
        self.in_remarkable: bool = False

        # Distinct de `in_remarkable` : indique si reMarkable est utilisable
        # du tout (rmapi.exe présent), indépendamment d'où on se trouve.
        self.remarkable: Optional[RemarkableBackend] = (
            RemarkableBackend() if RMAPI_PATH and os.path.isfile(RMAPI_PATH) else None
        )

    @property
    def location(self) -> str:
        if self.in_remarkable:
            assert self.remarkable is not None
            return f"reMarkable:{self.remarkable.pwd()}"
        return self.local_location

    def get_working_directory(self) -> str:
        return self.location

    @staticmethod
    def _strip_remote_prefix(path: str) -> Optional[str]:
        """
        Si `path` désigne un chemin sur reMarkable ('reMarkable:xxx',
        '/reMarkable/xxx' ou '/reMarkable'), retourne le chemin distant nu
        (insensible à la casse). Sinon retourne None.
        """
        lowered = path.lower()
        for prefix in ("remarkable:", "/remarkable/", "/remarkable"):
            if lowered.startswith(prefix):
                remainder = path[len(prefix):]
                return remainder if remainder else "."
        return None

    def _expand_path(self, path: str) -> str:
        if not self.in_remarkable:
            return os.path.expanduser(path).replace("\\", "/")
        return path.replace("\\", "/")

    def _require_remarkable(self) -> RemarkableBackend:
        if self.remarkable is None:
            raise RemarkableUnavailableError(
                "reMarkable n'est pas configuré (RMAPI_PATH manquant ou invalide)."
            )
        return self.remarkable

    # -- Navigation ---------------------------------------------------------- #

    def change_directory(self, path: str) -> None:
        raw = path

        # Cas 1 : "~" ou "~/..." ramène toujours au système de fichiers
        # local, même si on est actuellement dans reMarkable.
        if raw == "~" or raw.startswith("~/") or raw.startswith("~\\"):
            home_target = os.path.expanduser(raw).replace("\\", "/")
            if os.path.isfile(home_target):
                raise NotADirectoryError(home_target)
            if not os.path.isdir(home_target):
                raise FileNotFoundError(home_target)
            os.chdir(home_target)
            self.in_remarkable = False
            self.local_location = os.getcwd().replace("\\", "/")
            return

        target = self._expand_path(path)

        # Cas 2 : on est en local et on entre dans reMarkable (accepte aussi
        # "/reMarkable/sous/dossier" en un seul cd).
        if not self.in_remarkable:
            remote_target = self._strip_remote_prefix(target)
            if remote_target is not None:
                remarkable = self._require_remarkable()
                self.in_remarkable = True
                remarkable.path = "/"
                if remote_target not in (".", ""):
                    try:
                        remarkable.cd(remote_target)
                    except Exception:
                        self.in_remarkable = False
                        raise
                return

        # Cas 3 : on est déjà sur la tablette.
        if self.in_remarkable:
            remarkable = self._require_remarkable()
            remarkable.cd(target)
            return

        # Cas 4 : navigation locale classique.
        if os.path.isfile(target):
            raise NotADirectoryError(target)
        if not os.path.isdir(target):
            raise FileNotFoundError(target)
        os.chdir(target)
        self.local_location = os.getcwd().replace("\\", "/")

    # -- Copie ---------------------------------------------------------------- #

    def copy(self, src: str, dst: str, recursive: bool) -> None:
        # --- CAS 1 : copie interne à reMarkable ---
        # rmapi ne supporte pas 'cp' directement : on relaie via un
        # téléchargement local temporaire, suivi d'un envoi.
        if self.in_remarkable:
            remarkable = self._require_remarkable()
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

        # --- CAS 2 : on est en local ---

        # Sous-cas A : destination = reMarkable
        remote_dst = self._strip_remote_prefix(dst)
        if remote_dst is not None:
            remarkable = self._require_remarkable()
            local_src = self._expand_path(src)
            remarkable.put(local_src, remote_dst)
            return

        # Sous-cas B : source = reMarkable
        remote_src = self._strip_remote_prefix(src)
        if remote_src is not None:
            remarkable = self._require_remarkable()
            local_dst = self._expand_path(dst)
            remarkable.get(remote_src, local_dst)
            return

        # Sous-cas C : copie locale classique
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
        if self.in_remarkable:
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
        if depth == 0:
            return []

        res: List[str] = []
        try:
            # re.IGNORECASE reproduit le comportement "lower()" d'origine.
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            # Pattern regex invalide : on retombe sur une recherche littérale.
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
            pass  # Ignorer les dossiers sans permission

        return res

    def find(
        self, pattern: str, path: str, depth: int, strict: bool
    ) -> List[str]:
        if not pattern:
            raise ValueError("find: pattern must not be empty.")

        # Détection automatique : si le pattern finit par / ou \ on cherche
        # des dossiers uniquement.
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
        if self.in_remarkable:
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
        if self.in_remarkable:
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
