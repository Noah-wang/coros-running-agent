"""FIT 归档的目录名，以及去掉 endTimestamp 之后的迁移。

线上事故：同一次跑步存了 5 份，最早那份只有完整版的三分之一大。
原因是目录名里带了 endTimestamp——手表还在同步时它每轮都在变，
去重永远命中不了，于是每轮新建目录、重下一遍，半截文件留在归档里。
"""

import json
import os
import tempfile

import pytest

os.environ.setdefault("COROS_RUNTIME_SETTINGS_PATH", tempfile.mktemp())

from agents.coros_report import fit_archive as fa  # noqa: E402

ACTIVITY = {
    "labelId": "479836472606753074",
    "sportType": 100,
    "date": "2026-08-23",
    "startTimestamp": 1787529281,
    "endTimestamp": 1787530152,
}


def test_slug_ignores_end_timestamp():
    """同一次运动，endTimestamp 变了也必须算同一个目录——否则就会重下。"""
    still_syncing = dict(ACTIVITY, endTimestamp=1787530152)
    finished = dict(ACTIVITY, endTimestamp=1787539813)
    assert fa._activity_slug(still_syncing) == fa._activity_slug(finished)


def test_slug_still_separates_different_activities():
    other = dict(ACTIVITY, labelId="480137651821772802")
    assert fa._activity_slug(ACTIVITY) != fa._activity_slug(other)


def test_slug_separates_same_label_different_start():
    other = dict(ACTIVITY, startTimestamp=1787600000)
    assert fa._activity_slug(ACTIVITY) != fa._activity_slug(other)


# ── 迁移 ──────────────────────────────────────────────────────────────

FIT_HEADER = b"\x0e\x10\x00\x00\x00\x00\x00\x00.FIT"


def _make_dir(base, name, activity, fit_bytes):
    folder = base / activity["date"] / name
    folder.mkdir(parents=True)
    (folder / f"{activity['labelId']}.fit").write_bytes(FIT_HEADER + fit_bytes)
    (folder / "metadata.json").write_text(
        json.dumps({"activity": activity}), encoding="utf-8"
    )
    return folder


@pytest.fixture
def archive(tmp_path, monkeypatch):
    base = tmp_path / "fit-files"
    base.mkdir()
    monkeypatch.setattr(fa, "_base_fit_dir", lambda: base)
    return base


def test_migration_keeps_the_largest_and_drops_the_partials(archive, monkeypatch):
    """照线上真实那组构造：33KB 半截 + 四份 97KB 完整版。"""
    import scripts.migrate_fit_archive as mig

    monkeypatch.setattr(mig, "_base_fit_dir", lambda: archive)

    partial = _make_dir(archive, "2026-08-23-479836472606753074-100-1787529281-1787530152",
                        dict(ACTIVITY, endTimestamp=1787530152), b"x" * 33929)
    for end in (1787531388, 1787532054, 1787538764):
        _make_dir(archive, f"2026-08-23-479836472606753074-100-1787529281-{end}",
                  dict(ACTIVITY, endTimestamp=end), b"x" * 97477)
    biggest = _make_dir(archive, "2026-08-23-479836472606753074-100-1787529281-1787539813",
                        dict(ACTIVITY, endTimestamp=1787539813), b"x" * 99999)

    renames, deletions = mig.plan(archive)
    deleted = {folder for folder, _, _ in deletions}

    assert partial in deleted, "半截文件必须被删掉"
    assert biggest not in deleted, "最完整的那份必须留下"
    assert len(deletions) == 4
    assert renames == [(biggest, archive / "2026-08-23"
                        / "2026-08-23-479836472606753074-100-1787529281")]


def test_migration_is_idempotent(archive, monkeypatch):
    """迁移过一次之后再跑，不该再有任何动作。"""
    import scripts.migrate_fit_archive as mig

    monkeypatch.setattr(mig, "_base_fit_dir", lambda: archive)
    _make_dir(archive, "2026-08-23-479836472606753074-100-1787529281",
              ACTIVITY, b"x" * 97477)
    renames, deletions = mig.plan(archive)
    assert renames == []
    assert deletions == []


def test_migration_skips_dirs_without_metadata(archive, monkeypatch, capsys):
    """没有元数据就不猜。按目录名反推是有损的，宁可留着让人看一眼。"""
    import scripts.migrate_fit_archive as mig

    monkeypatch.setattr(mig, "_base_fit_dir", lambda: archive)
    folder = archive / "2026-08-23" / "2026-08-23-somelabel-100-123-456"
    folder.mkdir(parents=True)
    (folder / "a.fit").write_bytes(FIT_HEADER)

    renames, deletions = mig.plan(archive)
    assert renames == [] and deletions == []
    assert folder.exists()
    assert "没有 metadata.json" in capsys.readouterr().out


def test_invalid_fit_is_detected():
    """保留哪一份之前要能判断文件本身是不是合法 FIT。"""
    import scripts.migrate_fit_archive as mig
    import tempfile as tf
    from pathlib import Path

    with tf.TemporaryDirectory() as d:
        good = Path(d) / "good.fit"
        good.write_bytes(FIT_HEADER + b"payload")
        bad = Path(d) / "bad.fit"
        bad.write_bytes(b"<html>404 not found</html>")
        assert mig.is_valid_fit(good) is True
        assert mig.is_valid_fit(bad) is False
