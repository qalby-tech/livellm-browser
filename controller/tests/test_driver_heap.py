"""Each engine's Node driver gets an equal share of the pod's heap cap."""
import os

import patchright._impl._transport as chrome_transport
import playwright._impl._transport as camoufox_transport

from core import pw


def test_both_transports_start_their_driver_with_a_share():
    # A client upgrade that renames the hook must fail here, not quietly give
    # each driver the whole cap again.
    assert getattr(chrome_transport.get_driver_env, "splits_heap", False)
    assert getattr(camoufox_transport.get_driver_env, "splits_heap", False)


def test_the_driver_env_carries_the_share(monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--max-old-space-size=3072")
    for t in (chrome_transport, camoufox_transport):
        env = t.get_driver_env()
        assert env["NODE_OPTIONS"] == "--max-old-space-size=1536"
        assert env["PW_LANG_NAME"] == "python"  # the client's own env is kept
    assert os.environ["NODE_OPTIONS"] == "--max-old-space-size=3072"  # untouched


def test_without_node_options_the_env_is_unchanged(monkeypatch):
    monkeypatch.delenv("NODE_OPTIONS", raising=False)
    assert "NODE_OPTIONS" not in chrome_transport.get_driver_env()


def test_rewrite():
    f = pw.driver_node_options
    assert f(None) is None
    assert f("") == ""
    assert f("--max-old-space-size=1024") == "--max-old-space-size=512"
    assert f("--max_old_space_size=8192") == "--max_old_space_size=4096"
    assert f("--trace-warnings --max-old-space-size=6145 --enable-source-maps") == (
        "--trace-warnings --max-old-space-size=3072 --enable-source-maps"
    )
    assert f("--trace-warnings") == "--trace-warnings"
    assert f("--max-old-space-size=1") == "--max-old-space-size=1"
