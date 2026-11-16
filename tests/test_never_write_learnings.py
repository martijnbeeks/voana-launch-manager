"""kill-sync must never write a learnings field: the team fills them in."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("kill_sync", Path(__file__).resolve().parents[1] / "scripts" / "kill_sync.py")
kill_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kill_sync)


@pytest.mark.parametrize("fid", sorted(kill_sync.NEVER_WRITE_FIELDS))
def test_learnings_fields_are_never_written(fid):
    with pytest.raises(ValueError):
        kill_sync.cu_set_field("T1", fid, "anything", dry=True)


def test_configured_learnings_field_is_guarded():
    import json
    cfg = json.loads((Path(kill_sync.__file__).parent / "kill_sync_config.json").read_text())
    assert cfg["clickup"]["fields"]["learnings"] in kill_sync.NEVER_WRITE_FIELDS
