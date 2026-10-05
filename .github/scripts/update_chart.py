#!/usr/bin/env python3
"""Write this repo's image versions into the operator chart's Chart.yaml.

    update_chart.py Chart.yaml --app V --controller V --camoufox V

Each value lives on one anchored line:

    appVersion: "<v>"                  (top level: the Chrome image's tag)
      controllerVersion: "<v>"         (under annotations: the Browser API's)
      camoufoxVersion: "<v>"           (under annotations: the Camoufox image's)

Only a line whose value changed is rewritten; a missing annotation is
inserted at the end of the annotations: block. Every other byte of the file
(comments, the chart's own version:, the order) stays as it was. Prints
"changed=true|false" for the workflow; exits non-zero when the file has no
appVersion line or no annotations: block, rather than guess.
"""
import argparse
import re
import sys

TOP = ("appVersion", "app")
ANNOTATIONS = (
    ("controllerVersion", "controller"),
    ("camoufoxVersion", "camoufox"),
)


def line_re(key: str, indent: str) -> "re.Pattern[str]":
    return re.compile(rf'^{indent}{key}:\s*"?([^"\n]*)"?\s*$')


def update(text: str, values: dict) -> "tuple[str, list[str]]":
    lines = text.split("\n")
    changed = []

    def set_at(i: int, key: str, indent: str, value: str) -> None:
        m = line_re(key, indent).match(lines[i])
        if m and m.group(1) == value:
            return
        lines[i] = f'{indent}{key}: "{value}"'
        changed.append(key)

    top_key, top_arg = TOP
    idx = [i for i, l in enumerate(lines) if line_re(top_key, "").match(l)]
    if len(idx) != 1:
        raise SystemExit(f"Chart.yaml: expected one ^{top_key}: line, found {len(idx)}")
    set_at(idx[0], top_key, "", values[top_arg])

    starts = [i for i, l in enumerate(lines) if re.match(r"^annotations:\s*$", l)]
    if len(starts) != 1:
        raise SystemExit(f"Chart.yaml: expected one ^annotations: block, found {len(starts)}")
    start = starts[0]
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or lines[end].strip() == ""):
        end += 1
    # The block's last real line (trailing blank lines stay after it).
    last = end - 1
    while last > start and lines[last].strip() == "":
        last -= 1

    for key, arg in ANNOTATIONS:
        rx = line_re(key, "  ")
        found = [i for i in range(start + 1, end) if rx.match(lines[i])]
        if len(found) > 1:
            raise SystemExit(f"Chart.yaml: {key} appears {len(found)} times")
        if found:
            set_at(found[0], key, "  ", values[arg])
        else:
            last += 1
            lines.insert(last, f'  {key}: "{values[arg]}"')
            end += 1
            changed.append(key)
    return "\n".join(lines), changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chart")
    ap.add_argument("--app", required=True)
    ap.add_argument("--controller", required=True)
    ap.add_argument("--camoufox", required=True)
    a = ap.parse_args()
    with open(a.chart, encoding="utf-8") as f:
        text = f.read()
    new, changed = update(text, vars(a))
    if changed:
        with open(a.chart, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"updated: {', '.join(changed)}", file=sys.stderr)
    else:
        print("versions unchanged", file=sys.stderr)
    print(f"changed={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
