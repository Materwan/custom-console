"""The description of a tool that the model reads: JSON schema built from the function.

The name and the type hints of the parameters give the schema; the docstring gives the
description (everything before ``Args:``) and the description of each parameter (the
``Args:`` block, one ``name: text`` entry per parameter, continued on indented lines).
"""

from __future__ import annotations

import inspect
import json
import re
import types
import typing
from typing import Any, Callable, Dict, Iterable, List, Literal, Tuple, Union, get_args, get_origin

_ENTRY = re.compile(r"^(\w+)(?:\s*\([^)]*\))?:\s*(.*)$")


def _split_doc(doc: str) -> Tuple[str, List[str]]:
    """The description and the lines of the ``Args:`` block."""
    lines = doc.splitlines()
    for number, line in enumerate(lines):
        if line.strip() == "Args:":
            return "\n".join(lines[:number]), lines[number + 1 :]
    return doc, []


def _paragraphs(text: str) -> str:
    """Lines of a paragraph joined by spaces; paragraphs kept apart."""
    paragraphs = [" ".join(part.split()) for part in re.split(r"\n\s*\n", text.strip())]
    return "\n\n".join(part for part in paragraphs if part)


def _parameter_docs(lines: Iterable[str], names: Iterable[str]) -> Dict[str, str]:
    known = set(names)
    docs: Dict[str, List[str]] = {}
    current = None
    base = None
    for line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if base is None:
            base = indent
        entry = _ENTRY.match(line.strip()) if indent <= base else None
        if entry and entry.group(1) in known:
            current = entry.group(1)
            docs[current] = [entry.group(2)]
        elif current is not None:
            docs[current].append(line.strip())
    return {name: " ".join(" ".join(parts).split()) for name, parts in docs.items()}


def _type_schema(hint: Any) -> Dict[str, Any]:
    if hint is Any or hint is inspect.Parameter.empty:
        return {}
    if hint is str:
        return {"type": "string"}
    if hint is bool:
        return {"type": "boolean"}
    if hint is int:
        return {"type": "integer"}
    if hint is float:
        return {"type": "number"}
    if hint is type(None):
        return {"type": "null"}
    origin, args = get_origin(hint), get_args(hint)
    if origin is Literal:
        return {"enum": list(args)}
    if origin in (list, set, frozenset, tuple):
        return {"type": "array", "items": _type_schema(args[0]) if args else {}}
    if origin is dict or hint is dict:
        return {"type": "object"}
    if origin is Union or origin is types.UnionType:
        options = [arg for arg in args if arg is not type(None)]
        if len(options) == 1:
            return _type_schema(options[0])
        return {"anyOf": [_type_schema(option) for option in options]}
    return {"type": "string"}


_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off", "")


def _coerce(hint: Any, value: Any) -> Any:
    """`value` converted to `hint` when a model sent it in another JSON type ("2" for 2, "true" for
    true, one item for a list...). Anything that does not convert cleanly is left as it is: the tool
    then reports the problem."""
    if value is None or hint is Any or hint is inspect.Parameter.empty:
        return value
    origin, args = get_origin(hint), get_args(hint)
    if origin is Union or origin is types.UnionType:
        options = [arg for arg in args if arg is not type(None)]
        return _coerce(options[0], value) if len(options) == 1 else value
    try:
        if hint is bool and isinstance(value, str):
            lowered = value.strip().lower()
            return True if lowered in _TRUE else False if lowered in _FALSE else value
        if hint is bool and isinstance(value, int):
            return bool(value)
        if hint is int and not isinstance(value, bool):
            if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
                return int(value.strip())
            if isinstance(value, float) and value.is_integer():
                return int(value)
        if hint is float and isinstance(value, (str, int)) and not isinstance(value, bool):
            return float(value)
        if hint is str and isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if origin in (list, tuple, set) or hint in (list, tuple, set):
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.startswith("["):
                    value = json.loads(stripped)
            if not isinstance(value, (list, tuple)):
                value = [value]
            return [_coerce(args[0], item) for item in value] if args else list(value)
        if (origin is dict or hint is dict) and isinstance(value, str) and value.strip().startswith("{"):
            return json.loads(value)
    except (ValueError, TypeError):
        return value
    return value


def coerce_arguments(function: Callable[..., Any], arguments: Dict[str, Any]) -> Dict[str, Any]:
    """The arguments of a call, each converted to its parameter's type hint when it can be."""
    target = inspect.unwrap(function)
    try:
        hints = typing.get_type_hints(target)
    except Exception:
        return dict(arguments)
    return {name: _coerce(hints.get(name, Any), value) for name, value in arguments.items()}


def tools_token_estimate(tools: Iterable[Callable[..., Any]]) -> int:
    """Rough size of the tool definitions sent with every request."""
    from .context import estimate_tokens

    return sum(estimate_tokens(json.dumps(tool_schema(tool), ensure_ascii=False)) for tool in tools)


def tool_schema(tool: Callable[..., Any]) -> Dict[str, Any]:
    """The function-calling schema of `tool` (``{"type": "function", "function": {...}}``)."""
    target = inspect.unwrap(tool)
    signature = inspect.signature(target)
    try:
        hints = typing.get_type_hints(target)
    except Exception:  # an annotation that cannot be resolved: fall back to plain strings
        hints = {}

    description, arg_lines = _split_doc(inspect.getdoc(target) or "")
    docs = _parameter_docs(arg_lines, signature.parameters)

    properties: Dict[str, Any] = {}
    required: List[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        schema = _type_schema(hints.get(name, parameter.annotation))
        if name in docs:
            schema = {**schema, "description": docs[name]}
        properties[name] = schema
        if parameter.default is parameter.empty:
            required.append(name)

    return {
        "type": "function",
        "function": {
            "name": tool.__name__,
            "description": _paragraphs(description),
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }
