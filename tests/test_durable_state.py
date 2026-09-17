"""Durable state must live in a TRACKED directory.

On the Mac Mini every Multica run is a fresh checkout. While the ledger and the
poll window sat under the gitignored state/, a fresh checkout would have reset
the window to "now minus 36h" and forgotten every kill already reported.
"""

import importlib.util
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("kill_sync", _ROOT / "scripts" / "kill_sync.py")
ks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ks)


def test_durable_state_is_not_gitignored():
    assert ks.DURABLE == _ROOT / "data" / "kill-sync"
    for name in ("kills.jsonl", "last_run.json"):
        ignored = subprocess.run(["git", "check-ignore", "-q", str(ks.DURABLE / name)], cwd=_ROOT)
        assert ignored.returncode == 1, f"{name} is gitignored — a fresh checkout would lose it"


def test_ledger_and_window_use_the_durable_dir():
    src = (_ROOT / "scripts" / "kill_sync.py").read_text()
    assert 'STATE / "kills.jsonl"' not in src
    assert 'STATE / "last_run.json"' not in src


def test_prompt_and_wrapper_read_the_same_window_file():
    prompt = (_ROOT / "scripts" / "kill_sync_prompt.md").read_text()
    wrapper = (_ROOT / "scripts" / "run_kill_sync.sh").read_text()
    assert "data/kill-sync/last_run.json" in prompt
    assert "data/kill-sync/last_run.json" in wrapper
    assert "push-state" in prompt


def test_prompt_is_runtime_independent():
    """The Meta tool prefix differs between the Mac Mini and the MacBook."""
    prompt = (_ROOT / "scripts" / "kill_sync_prompt.md").read_text()
    assert "/Users/martijnbeeks" not in prompt
    assert "ToolSearch" in prompt


def test_push_state_pushes_head_to_main(tmp_path, monkeypatch):
    """The Multica checkout sits on a throwaway branch, so it must be HEAD:main."""
    calls = []

    class R:
        def __init__(self, out=""):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    def fake_run(cmd, **kw):
        calls.append(cmd[1:])
        return R(" M data/kill-sync/last_run.json" if cmd[1] == "status" else "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ks.push_state() == 0
    assert ["push", "-q", "origin", "HEAD:main"] in calls
    assert any(c[:2] == ["pull", "--rebase"] for c in calls)


def test_push_state_is_a_noop_when_nothing_changed(monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd[1:]) or R())
    assert ks.push_state() == 0
    assert not any(c[0] == "push" for c in calls)
