"""serve.js's own tests (tests/serve_test.js), on the playwright package's
node: the one serve.js runs on in the image."""
import subprocess
from pathlib import Path

from core.server import SERVE_JS, driver_paths


def test_serve_js():
    node, _ = driver_paths()
    test = Path(__file__).resolve().parent / "serve_test.js"
    r = subprocess.run([node, "--test", "--test-reporter=tap", str(test)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "# fail 0" in r.stdout


def test_serve_js_parses():
    node, _ = driver_paths()
    subprocess.run([node, "--check", str(SERVE_JS)], check=True, timeout=60)
