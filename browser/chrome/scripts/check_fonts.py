"""Build-time check: every offered locale resolves to its table font.

Run in the image: python scripts/check_fonts.py /etc/livellm/locales.json
"""
import json
import subprocess
import sys


def main(path):
    table = json.load(open(path))
    bad = []
    for tag, row in sorted(table.items()):
        # fontconfig knows zh-cn / zh-tw, but only the bare language for the rest.
        lang = tag.lower() if tag.startswith("zh-") else tag.split("-")[0].lower()
        got = subprocess.run(
            ["fc-match", "-f", "%{family}", ":lang=" + lang],
            capture_output=True, text=True,
        ).stdout
        print(f"{tag}: {got}")
        if row["font"] not in got:
            bad.append(f"{tag}: got {got!r}, want {row['font']!r}")
    if bad:
        print("font check failed:\n" + "\n".join(bad))
        return 1
    print("fonts ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
