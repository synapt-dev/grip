#!/usr/bin/env python3
"""Generate ``docs/GLOSSARY.md`` from ``docs/vocabulary.json``.

The glossary is generated so the words and the commands beside them are written once. Run it
after editing ``docs/vocabulary.json``; ``--check`` exits 1 when the committed glossary is not
what this script would write, and ``tests/test_vocabulary.py`` runs that same comparison.

    python3 gr2/scripts/gen_vocabulary_glossary.py            # write docs/GLOSSARY.md
    python3 gr2/scripts/gen_vocabulary_glossary.py --check    # fail if it is out of date
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

GR2 = Path(__file__).resolve().parents[1]
SOURCE = GR2 / "docs" / "vocabulary.json"
TARGET = GR2 / "docs" / "GLOSSARY.md"


def render(vocabulary: dict) -> str:
    lines = [
        "# gr2 glossary",
        "",
        "The words gr2 uses for its own commands, each with the command that does it. "
        "This file is generated from [vocabulary.json](vocabulary.json) by "
        "`scripts/gen_vocabulary_glossary.py`; edit the JSON and regenerate, not this file.",
        "",
    ]
    for entry in vocabulary["terms"]:
        commands = ", ".join(f"`gr2 {c}`" for c in entry["gr2"]) or "not a gr2 command"
        lines += [f"## {entry['term']}", "", entry["meaning"], "", f"Command: {commands}.", ""]
        if entry.get("note"):
            lines += [entry["note"], ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str]) -> int:
    expected = render(json.loads(SOURCE.read_text()))
    if "--check" in argv:
        if not TARGET.exists() or TARGET.read_text() != expected:
            print(f"{TARGET} is out of date: run scripts/gen_vocabulary_glossary.py", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(expected)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
