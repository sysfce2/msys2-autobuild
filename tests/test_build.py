from msys2_autobuild.build import get_packager


def test_get_packager(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("JOB_CHECK_RUN_ID", "67890")
    packager = get_packager("ucrt64")
    assert "https://github.com/msys2/msys2-autobuild/actions/runs/12345/job/67890" in packager
