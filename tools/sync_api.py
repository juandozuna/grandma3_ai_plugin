#!/usr/bin/env python3
"""Parse the console's HelpLua export into api_index.json.

Run `HelpLua` on the console (writes grandMA3_lua_functions.txt into gma3_library), then:
    python3 tools/sync_api.py [path/to/grandMA3_lua_functions.txt]
ma3_api(drift=true) compares this index with the live GetApiDescriptor output.
"""
import json
import os
import re
import sys

DEFAULT = os.path.expanduser("~/MALightingTechnology/gma3_library/grandMA3_lua_functions.txt")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api_index.json")


def parse(text: str) -> dict:
    index = {"fn": [], "obj": [], "signatures": {}}
    section = "fn"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "Object API":
            section = "obj"
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\((.*)$", stripped)
        if match:
            index[section].append(match.group(1))
            index["signatures"][section + ":" + match.group(1)] = stripped
    index["fn"] = sorted(set(index["fn"]))
    index["obj"] = sorted(set(index["obj"]))
    return index


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    with open(source, "r", encoding="utf-8", errors="replace") as handle:
        index = parse(handle.read())
    index["source"] = source
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False, indent=1)
    print("wrote %s: %d object-free, %d object functions" % (os.path.normpath(OUT), len(index["fn"]), len(index["obj"])))


if __name__ == "__main__":
    main()
