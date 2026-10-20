"""Builds the real prompt with all the local tools, in the worst case, and fails when it does not fit what Clara accepts.

Prints the numbers the README quotes: the number of tools, the characters of the base prompt, of the longest prompt the
console can send (every session fact on, the longest `AGENT.md` it reads) and of the tool schemas.
"""

from __future__ import annotations

import json
import sys

from custom_console.clara.types import MAX_INSTRUCTIONS, MAX_TOOLS
from custom_console.core.prompt import (
    BASE_LIMIT,
    PROJECT_FILE,
    PROJECT_FILE_LIMIT,
    SessionFacts,
    base_prompt,
    build_instructions,
)
from custom_console.core.tools.registry import ALL_TOOLS, PREFIX

SCHEMA_LIMIT = 12_000  # characters of tool schemas: they ride in every request, so they are kept small


def measure() -> dict[str, int]:
    long_path = "/C:/Users/" + "x" * 80 + "/projects/" + "y" * 80
    facts = SessionFacts("windows", long_path, "C:\\Users\\" + "x" * 80 + "\\projects\\" + "y" * 80, False, True, True, True,
                         tuple(spec.name for spec in ALL_TOOLS))
    project = "z" * (PROJECT_FILE_LIMIT + 500)  # the file reader cuts it; a longer one is cut here the same way
    cut = project[:PROJECT_FILE_LIMIT] + f"\n[... {PROJECT_FILE} cut at {PROJECT_FILE_LIMIT:,} characters ...]"
    schemas = sum(len(json.dumps(spec.schema())) for spec in ALL_TOOLS)
    return {"tools": len(ALL_TOOLS), "base": len(base_prompt()), "base_and_facts": len(build_instructions(facts)),
            "worst_case": len(build_instructions(facts, cut)), "schemas": schemas}


def problems(numbers: dict[str, int]) -> list[str]:
    found = []
    if numbers["worst_case"] > MAX_INSTRUCTIONS:
        found.append(f"the longest prompt is {numbers['worst_case']:,} characters; Clara takes {MAX_INSTRUCTIONS:,}")
    if numbers["base_and_facts"] > BASE_LIMIT:
        found.append(f"the base prompt and the session facts are {numbers['base_and_facts']:,} characters; the budget is {BASE_LIMIT:,}")
    if numbers["tools"] > MAX_TOOLS:
        found.append(f"{numbers['tools']} tools; Clara takes {MAX_TOOLS}")
    if numbers["schemas"] > SCHEMA_LIMIT:
        found.append(f"the tool schemas are {numbers['schemas']:,} characters; the budget is {SCHEMA_LIMIT:,}")
    found += [f"{spec.name} does not start with {PREFIX}" for spec in ALL_TOOLS if not spec.name.startswith(PREFIX)]
    return found


if __name__ == "__main__":
    numbers = measure()
    print(f"tools: {numbers['tools']}   schemas: {numbers['schemas']:,} characters")
    print(f"base prompt: {numbers['base']:,}   with the session facts: {numbers['base_and_facts']:,}   "
          f"longest possible: {numbers['worst_case']:,} of {MAX_INSTRUCTIONS:,}")
    errors = problems(numbers)
    for error in errors:
        print(f"check_prompt: {error}", file=sys.stderr)
    sys.exit(1 if errors else 0)
