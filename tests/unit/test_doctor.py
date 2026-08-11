from arctic_route_data.doctor import inspect_archive


def test_empty_archive_fails_unless_explicitly_allowed(tmp_path):
    strict = inspect_archive(tmp_path / "data")
    allowed = inspect_archive(tmp_path / "data", allow_empty=True)

    assert not strict.ok
    assert strict.errors == ("manifest 当前为空",)
    assert allowed.ok
    assert allowed.warnings == ("manifest 当前为空",)


def test_orphan_ready_file_is_an_error(tmp_path):
    path = tmp_path / "data" / "ready" / "orphan.nc"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"orphan")

    report = inspect_archive(tmp_path / "data", allow_empty=True)

    assert not report.ok
    assert any("未登记" in error for error in report.errors)


def test_repository_placeholders_are_not_reported_as_archive_content(tmp_path):
    root = tmp_path / "data"
    for directory in ("ready", "incoming"):
        path = root / directory / ".gitkeep"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n", encoding="utf-8")

    report = inspect_archive(root, allow_empty=True)

    assert report.ok
    assert report.errors == ()
    assert report.warnings == ("manifest 当前为空",)
