"""A fake `rmapi` for tests: run as `python fake_rmapi.py <command> <args>`.

It reads its state from the environment, so a test sets it up and nothing else is shared:

    FAKE_RMAPI_TREE   a JSON file: {"/Notes": {"type": "d"}, "/Notes/Plan": {"type": "f", "rmdoc": "<path>"}}
    FAKE_RMAPI_MODE   ok (default) | unauth | hang | error | notfound | rate
    FAKE_RMAPI_STATE  a folder for counters (the `rate` mode answers 429 for the first calls)
    FAKE_RMAPI_RATE   how many calls answer 429 before one succeeds (default 2)
    FAKE_RMAPI_LOG    a file where each call's argv is appended
"""

from __future__ import annotations

import json
import os
import posixpath
import shutil
import sys
import time
from pathlib import Path


def load() -> dict[str, dict[str, str]]:
    path = os.environ.get("FAKE_RMAPI_TREE")
    return json.loads(Path(path).read_text()) if path else {}


def save(tree: dict[str, dict[str, str]]) -> None:
    path = os.environ.get("FAKE_RMAPI_TREE")
    if path:
        Path(path).write_text(json.dumps(tree))


def children(tree: dict[str, dict[str, str]], folder: str) -> list[tuple[str, dict[str, str]]]:
    folder = posixpath.normpath(folder)
    return sorted((posixpath.basename(p), v) for p, v in tree.items()
                  if posixpath.dirname(p) == folder and p != "/")


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_RMAPI_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    mode = os.environ.get("FAKE_RMAPI_MODE", "ok")
    if mode == "unauth":
        sys.stdout.write("Enter one-time code (go to https://my.remarkable.com/device/desktop/connect): ")
        sys.stdout.flush()
        code = sys.stdin.readline()  # waits for ever on a terminal; an end of file when input is closed
        if not code.strip():
            print("\nERROR: no code given")
            return 1
    if mode == "hang":
        time.sleep(3600)
    if mode == "error":
        print("ERROR: failed to build documents tree: dial tcp: lookup internal.cloud.remarkable.com: no such host")
        return 1
    if mode == "rate":
        state = Path(os.environ["FAKE_RMAPI_STATE"]) / "count"
        count = int(state.read_text()) if state.exists() else 0
        state.write_text(str(count + 1))
        if count < int(os.environ.get("FAKE_RMAPI_RATE", "2")):
            print("ERROR: failed to fetch tree: HTTP 429 Too Many Requests")
            return 1
    if not argv:
        print("usage: rmapi <command>")
        return 1
    command, args = argv[0], argv[1:]
    tree = load()
    if command == "version":
        print("rmapi version: 0.0.26-fake")
        return 0
    if command == "ls":
        target = posixpath.normpath(args[0] if args else "/")
        if target != "/" and target not in tree:
            print(f"ERROR: directory doesn't exist {target}")
            return 1
        if target != "/" and tree[target]["type"] == "f":
            print(f"[f]\t{posixpath.basename(target)}")
            return 0
        for name, entry in children(tree, target):
            print(f"[{entry['type']}]\t{name}")
        return 0
    if command == "get":
        target = posixpath.normpath(args[0])
        if target not in tree or tree[target]["type"] != "f":
            print(f"ERROR: file doesn't exist {target}")
            return 1
        destination = Path.cwd() / f"{posixpath.basename(target)}.rmdoc"
        source = tree[target].get("rmdoc")
        if source:
            shutil.copyfile(source, destination)
        else:
            destination.write_bytes(b"PK-fake")
        print(f"[1/1] downloading: [{posixpath.basename(target)}]...")
        return 0
    if command == "put":
        local, remote = Path(args[0]), posixpath.normpath(args[1] if len(args) > 1 else "/")
        if remote != "/" and tree.get(remote, {}).get("type") != "d":
            print(f"ERROR: directory doesn't exist {remote}")
            return 1
        tree[posixpath.join(remote, local.stem)] = {"type": "f"}
        save(tree)
        print(f"uploading: [{local.name}]...OK")
        return 0
    if command == "mkdir":
        target = posixpath.normpath(args[0])
        if target in tree:
            print(f"ERROR: entry already exists {target}")
            return 1
        tree[target] = {"type": "d"}
        save(tree)
        return 0
    if command == "rm":
        target = posixpath.normpath(args[0])
        if target not in tree:
            print(f"ERROR: file doesn't exist {target}")
            return 1
        if tree[target]["type"] == "d" and children(tree, target):
            print("ERROR: directory is not empty")
            return 1
        del tree[target]
        save(tree)
        return 0
    if command == "mv":
        source, dest = posixpath.normpath(args[0]), posixpath.normpath(args[1])
        if source not in tree:
            print(f"ERROR: file doesn't exist {source}")
            return 1
        if tree.get(dest, {}).get("type") == "d":
            dest = posixpath.join(dest, posixpath.basename(source))
        tree[dest] = tree.pop(source)
        save(tree)
        return 0
    print(f"ERROR: unknown command {command}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
