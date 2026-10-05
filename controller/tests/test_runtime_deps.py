"""The image installs only [project].dependencies (uv sync with UV_NO_DEV=1),
while the tests run with the dev group too: a package the controller imports
but only the dev group lists passes every test here and stops the container at
start (ModuleNotFoundError). These read pyproject.toml and the code the image
runs, so that can't ship again."""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
IMPORT_NAME = {"pytest-asyncio": "pytest_asyncio", "beautifulsoup4": "bs4", "pydantic-settings": "pydantic_settings"}


def _names(block):
    return {re.split(r"[<>=!~\[; ]", s.strip())[0].lower() for s in re.findall(r'"([^"]+)"', block)}


def _deps():
    text = (ROOT / "pyproject.toml").read_text()
    runtime = _names(re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M).group(1))
    dev = _names(re.search(r"^dev\s*=\s*\[(.*?)\]", text, re.S | re.M).group(1))
    return runtime, dev - runtime


def _image_imports():
    """top-level module -> the files of the image's code that import it"""
    out = {}
    for f in ROOT.rglob("*.py"):
        rel = f.relative_to(ROOT)
        if rel.parts[0] in ("tests", ".venv") or "__pycache__" in rel.parts:
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            else:
                continue
            for m in mods:
                out.setdefault(m.split(".")[0], set()).add(str(rel))
    return out


def _module(dist):
    return IMPORT_NAME.get(dist, dist.replace("-", "_"))


def test_the_image_code_imports_no_dev_only_package():
    _, dev_only = _deps()
    imports = _image_imports()
    bad = {d: sorted(imports[_module(d)]) for d in dev_only if _module(d) in imports}
    assert not bad, f"imported by the image's code but listed only in the dev group: {bad}"


def test_the_sidecar_client_is_a_runtime_dependency():
    runtime, _ = _deps()
    assert "httpx" in runtime  # core/keeper_hint.py reaches the browser's control sidecar with it
    assert "httpx" in _image_imports()


# ── The Camoufox Browser API image (Dockerfile.camoufox) ──
#
# It installs requirements-camoufox.txt (hashed, from requirements-camoufox.in)
# instead of pyproject.toml: the same runtime dependencies with stock
# Playwright in place of patchright. A dependency added to pyproject.toml
# and not to requirements-camoufox.in would pass every test and stop that
# image at start.

def _camoufox_in():
    lines = (ROOT / "requirements-camoufox.in").read_text().splitlines()
    reqs = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    return {re.split(r"[<>=!~\[; ]", r)[0].lower(): r for r in reqs}


def test_the_camoufox_image_has_the_same_runtime_dependencies():
    runtime, _ = _deps()
    want = (runtime - {"patchright"}) | {"playwright"}
    assert set(_camoufox_in()) == want


def test_the_camoufox_image_pins_one_playwright_with_hashes():
    pin = _camoufox_in()["playwright"]
    assert re.fullmatch(r"playwright==\d+\.\d+\.\d+", pin), pin
    txt = (ROOT / "requirements-camoufox.txt").read_text()
    assert re.search(rf"^{re.escape(pin)} \\$", txt, re.M), "requirements-camoufox.txt is not compiled from requirements-camoufox.in"
    for block in re.split(r"\n(?=[a-z0-9])", txt.split("\n", 2)[2] if txt.startswith("#") else txt):
        if re.match(r"^[a-z0-9]", block):
            assert "--hash=sha256:" in block, f"no hash: {block.splitlines()[0]}"


def test_playwright_is_imported_only_through_the_shim():
    imports = _image_imports()
    assert imports.get("playwright") == {"core/pw.py"}
    assert imports.get("patchright") == {"core/pw.py"}
