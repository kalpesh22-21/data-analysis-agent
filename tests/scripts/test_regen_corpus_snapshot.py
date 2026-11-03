import datetime as dt
import hashlib

import pytest
import yaml
from scripts.regen_corpus_snapshot import render


def _canon(tmp_path, docs):
    for doc in docs:
        (tmp_path / f"{doc['id']}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    manifest = "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in sorted(tmp_path.glob("*.yaml"))
    )
    (tmp_path / "MANIFEST.sha256").write_text(manifest)
    (tmp_path / "BLUEPRINTS_SHA").write_text(hashlib.sha1(manifest.encode()).hexdigest() + "\n")
    return tmp_path


def test_snapshot_copies_sealed_canon_verbatim_with_its_fingerprint(tmp_path):
    docs = [{"id": "bp-b", "intent": "Second."}, {"id": "bp-a", "intent": "First.", "slots": []}]
    canon = _canon(tmp_path, docs)
    text = render(canon, today=dt.date(2026, 11, 2))
    lines = text.splitlines()
    assert lines[0].endswith("synchronized 2026-11-02 (2 blueprints).")
    assert lines[1] == "# BLUEPRINTS_SHA: " + (canon / "BLUEPRINTS_SHA").read_text().strip()
    assert yaml.safe_load(text) == sorted(docs, key=lambda d: d["id"])


def test_unsealed_canon_is_refused(tmp_path):
    canon = _canon(tmp_path, [{"id": "bp-a", "intent": "First."}])
    (canon / "bp-a.yaml").write_text("id: bp-a\nintent: Edited without --write.\n")
    with pytest.raises(SystemExit, match="unsealed"):
        render(canon, today=dt.date(2026, 11, 2))
