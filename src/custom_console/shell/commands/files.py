"""File system commands: cd, ls, tree, cat, stat, find, cp, rm, pwd, rmdoc2pdf."""

from __future__ import annotations

import argparse

from rich.text import Text

from ...fs.rmdoc import RmdocError, rmdoc_to_pdf
from ..printer import COLOR_PATH, permission_flags
from .base import PATH, Command, CommandError, CommandRegistry, ShellContext, ShellParser


def _paths(parser: ShellParser, *, required: bool = False) -> None:
    if required:
        parser.add_argument("paths", nargs="+", metavar=PATH)
    else:
        parser.add_argument("paths", nargs="*", default=["."], metavar=PATH)


def _heading(ctx: ShellContext, args: argparse.Namespace, path: str) -> None:
    if len(args.paths) > 1:
        ctx.printer.plain(f"{path}:")


# -- cd / pwd ------------------------------------------------------------------ #


def _cd_args(parser: ShellParser) -> None:
    parser.add_argument("path", nargs="?", default=".", metavar=PATH)


def _cd(ctx: ShellContext, args: argparse.Namespace) -> None:
    ctx.files.change_directory(args.path)


def _pwd(ctx: ShellContext, args: argparse.Namespace) -> None:
    ctx.printer.plain(ctx.files.location, COLOR_PATH)


# -- ls / tree ------------------------------------------------------------------ #


def _ls_args(parser: ShellParser) -> None:
    _paths(parser)
    parser.add_argument("-a", "--all", action="store_true", help="include hidden entries")


def _ls(ctx: ShellContext, args: argparse.Namespace) -> None:
    for path in args.paths:
        entries = ctx.files.listdir(path, args.all)
        _heading(ctx, args, path)
        ctx.printer.listing(entries)


def _tree_args(parser: ShellParser) -> None:
    _paths(parser)
    parser.add_argument("-a", "--all", action="store_true", help="include hidden entries")
    parser.add_argument(
        "-d", "--depth", type=int, default=3, help="maximum depth (-1 = unlimited)"
    )


def _tree(ctx: ShellContext, args: argparse.Namespace) -> None:
    for path in args.paths:
        lines = ctx.files.tree(path, args.depth, args.all)
        _heading(ctx, args, path)
        for line in lines:
            ctx.printer.tree_line(line)


# -- cat / stat ------------------------------------------------------------------ #


def _cat_args(parser: ShellParser) -> None:
    _paths(parser, required=True)


def _cat(ctx: ShellContext, args: argparse.Namespace) -> None:
    for path in args.paths:
        content = ctx.files.cat(path)
        _heading(ctx, args, path)
        ctx.printer.text(content)


def _stat_args(parser: ShellParser) -> None:
    _paths(parser)


def _stat(ctx: ShellContext, args: argparse.Namespace) -> None:
    for path in args.paths:
        perms = ctx.files.stat(path)
        line = Text(path, style=COLOR_PATH)
        line.append(" ")
        line.append_text(permission_flags(perms))
        ctx.printer.console.print(line)


# -- find ------------------------------------------------------------------------ #


def _find_args(parser: ShellParser) -> None:
    parser.add_argument("pattern", help="regex; a trailing '/' matches directories only")
    parser.add_argument("path", nargs="?", default=".", metavar=PATH)
    parser.add_argument("-d", "--depth", type=int, default=5, help="maximum depth")
    parser.add_argument("-s", "--strict", action="store_true", help="match the whole name")


def _find(ctx: ShellContext, args: argparse.Namespace) -> None:
    with ctx.printer.status(f"Searching {args.pattern} in {args.path} (depth {args.depth})..."):
        results = ctx.files.find(args.pattern, args.path, args.depth, args.strict)
    if results:
        for result in results:
            ctx.printer.info(result)
    else:
        ctx.printer.warning("No match.")


# -- cp / rm --------------------------------------------------------------------- #


def _cp_args(parser: ShellParser) -> None:
    parser.add_argument("src", metavar=PATH)
    parser.add_argument("dst", metavar=PATH)
    parser.add_argument("-r", "--recursive", action="store_true", help="copy directories")


def _cp(ctx: ShellContext, args: argparse.Namespace) -> None:
    with ctx.printer.status("Starting...") as status:
        count = ctx.files.copy(
            args.src,
            args.dst,
            args.recursive,
            on_file=lambda file: status.update(f"Copied: {file}"),
        )
    ctx.printer.success(f"{count} file(s) copied.")


def _rm_args(parser: ShellParser) -> None:
    _paths(parser, required=True)
    parser.add_argument("-r", "--recursive", action="store_true", help="remove directories and their content")
    parser.add_argument("-f", "--force", action="store_true", help="do not ask for confirmation")


def _rm(ctx: ShellContext, args: argparse.Namespace) -> None:
    for path in args.paths:
        if args.recursive and not args.force:
            if not ctx.confirm(f"Recursively remove '{path}'?"):
                ctx.printer.warning(f"Skipped {path}.")
                continue
        ctx.files.remove(path, args.recursive)


# -- rmdoc2pdf ------------------------------------------------------------------- #


def _rmdoc_args(parser: ShellParser) -> None:
    parser.add_argument("source", metavar=PATH, help="the .rmdoc file (copied from the reMarkable)")
    parser.add_argument("output", nargs="?", metavar=PATH, help="PDF to create (default: next to the source)")
    parser.add_argument(
        "-o", "--original", action="store_true", help="only extract the original PDF, without the handwriting"
    )
    parser.add_argument("-f", "--force", action="store_true", help="replace the PDF if it exists")


def _rmdoc2pdf(ctx: ShellContext, args: argparse.Namespace) -> None:
    source = ctx.files.local_path(args.source, "rmdoc2pdf")
    target = ctx.files.local_path(args.output, "rmdoc2pdf") if args.output else None
    try:
        with ctx.printer.status("Converting..."):
            result = rmdoc_to_pdf(source, target, handwriting=not args.original, overwrite=args.force)
    except (RmdocError, ImportError) as error:
        raise CommandError(str(error)) from error
    detail = f"{result.pages} page(s)"
    if result.handwriting:
        detail += f", handwriting on {result.annotated_pages}"
    ctx.printer.success(f"{result.output} ({detail}).")
    for note in result.notes:
        ctx.printer.warning(note)


def register(registry: CommandRegistry) -> None:
    registry.add(Command("cd", "Change the current directory", _cd, _cd_args))
    registry.add(Command("pwd", "Print the current location", _pwd))
    registry.add(Command("ls", "List directory contents", _ls, _ls_args))
    registry.add(Command("tree", "Show a directory tree", _tree, _tree_args))
    registry.add(Command("cat", "Print file contents", _cat, _cat_args))
    registry.add(Command("stat", "Show r/w/x permissions", _stat, _stat_args))
    registry.add(Command("find", "Find entries by name (regex)", _find, _find_args))
    registry.add(Command("cp", "Copy files (local, WSL, reMarkable)", _cp, _cp_args))
    registry.add(Command("rm", "Remove files or directories", _rm, _rm_args))
    registry.add(Command("rmdoc2pdf", "Convert a reMarkable .rmdoc into a PDF", _rmdoc2pdf, _rmdoc_args))
