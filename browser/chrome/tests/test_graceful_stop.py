"""A pod stop (TERM) reaches the launcher and its lifespan shutdown runs in
the grace period: the desktop's startup.sh starts it as a simple background
command (so $! is uv, which passes TERM on), and uvicorn waits at most 3 s for
requests in flight before the browsers are closed."""
import ast
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
STARTUP = HERE.parent / "desktop" / "startup.sh"


def test_uvicorn_gives_requests_in_flight_3_seconds():
    tree = ast.parse((HERE / "launch.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "run" and getattr(n.func.value, "id", "") == "uvicorn"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("timeout_graceful_shutdown"), ast.Constant)
    assert kw["timeout_graceful_shutdown"].value == 3


def test_startup_sh_tracks_the_launcher_itself():
    text = STARTUP.read_text()
    lines = [l.strip() for l in text.splitlines() if "launch.py" in l and not l.strip().startswith(("#", "echo"))]
    assert lines == ["/bin/uv run launch.py 2>&1 &"], lines
    # a `cd … && uv …` list (or a subshell) forks: $! would be that shell, not uv
    assert not re.search(r"&&\s*/bin/uv run launch\.py", text)
    assert re.search(r"/bin/uv run launch\.py 2>&1 &\nAPP_PID=\$!", text)
