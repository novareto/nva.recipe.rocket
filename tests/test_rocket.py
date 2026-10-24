import os
import textwrap

import pytest
import zc.buildout.buildout
import zc.buildout.configparser
import zc.buildout.easy_install as easy_install

import nva.recipe.rocket as rocket


def make_package(base, name, src_layout=False):
    package = base / name
    code = package / "src" / name if src_layout else package / name
    code.mkdir(parents=True)
    (code / "__init__.py").write_text("VALUE = %r\n" % name)
    (package / "setup.py").write_text(
        textwrap.dedent(
            f"""\
            from setuptools import setup, find_packages
            setup(
                name={name!r},
                version="1.0",
                packages=find_packages({'"src"' if src_layout else ''}),
                {'package_dir={"": "src"},' if src_layout else ''}
            )
            """
        )
    )
    return package


@pytest.fixture
def project(tmp_path):
    make_package(tmp_path, "pkga")
    make_package(tmp_path, "pkgb", src_layout=True)
    (tmp_path / "buildout.cfg").write_text(
        textwrap.dedent(
            """\
            [buildout]
            develop = pkga pkgb
            parts =
            offline = true
            newest = false
            """
        )
    )
    return tmp_path


@pytest.fixture
def develop_calls(monkeypatch):
    calls = []
    original = easy_install.develop

    def develop(setup, dest, *args, **kw):
        calls.append(os.path.basename(setup))
        return original(setup, dest, *args, **kw)

    monkeypatch.setattr(easy_install, "develop", develop)
    return calls


def run_buildout(project, *options):
    """Run ``bin/buildout`` in-process; options look like ``section:key=value``."""
    cloptions = []
    for option in options:
        section, assignment = option.split(":", 1)
        cloptions.append((section, *assignment.split("=", 1)))
    cwd = os.getcwd()
    os.chdir(project)
    try:
        buildout = zc.buildout.buildout.Buildout(
            str(project / "buildout.cfg"), cloptions, use_user_defaults=False
        )
        rocket.extension(buildout)
        buildout.install([])
    finally:
        os.chdir(cwd)


def installed_develop_eggs(project):
    with open(project / ".installed.cfg") as fp:
        sections = zc.buildout.configparser.parse(fp, ".installed.cfg")
    return sections["buildout"]["installed_develop_eggs"].split()


def test_builds_then_reuses_cache(project, develop_calls):
    run_buildout(project)
    assert sorted(develop_calls) == ["pkga", "pkgb"]
    first = installed_develop_eggs(project)
    assert any("pkga" in p for p in first) and any("pkgb" in p for p in first)

    develop_calls.clear()
    run_buildout(project)
    assert develop_calls == []
    # Buildout removed the old files before developing; they are restored.
    assert installed_develop_eggs(project) == first
    assert all(os.path.exists(p) for p in first)


def test_changed_metadata_rebuilds_only_that_package(project, develop_calls):
    run_buildout(project)
    develop_calls.clear()

    setup_py = project / "pkga" / "setup.py"
    setup_py.write_text(setup_py.read_text().replace('"1.0"', '"1.1"'))
    run_buildout(project)
    assert develop_calls == ["pkga"]


def test_new_top_level_package_rebuilds(project, develop_calls):
    run_buildout(project)
    develop_calls.clear()

    (project / "pkgb" / "src" / "pkgb_extra").mkdir()
    run_buildout(project)
    assert develop_calls == ["pkgb"]


def test_cache_can_be_disabled(project, develop_calls):
    run_buildout(project, "buildout:rocket-cache=false")
    run_buildout(project, "buildout:rocket-cache=false")
    assert sorted(develop_calls) == ["pkga", "pkga", "pkgb", "pkgb"]


def test_sequential_worker(project, develop_calls):
    run_buildout(project, "buildout:rocket-workers=1")
    assert sorted(develop_calls) == ["pkga", "pkgb"]


def test_falls_back_to_build_isolation(project, monkeypatch):
    attempts = []
    original = easy_install.develop

    def develop(setup, dest, *args, **kw):
        isolated = not getattr(rocket._local, "no_isolation", False)
        attempts.append((os.path.basename(setup), isolated))
        if not isolated and setup.endswith("pkga"):
            raise rocket.NoIsolationFailed("backend missing")
        # Build every package without isolation, we are offline.
        rocket._local.no_isolation = True
        try:
            return original(setup, dest, *args, **kw)
        finally:
            rocket._local.no_isolation = not isolated

    monkeypatch.setattr(easy_install, "develop", develop)
    run_buildout(project, "buildout:rocket-workers=1")
    assert attempts == [("pkga", False), ("pkga", True), ("pkgb", False)]


def test_no_isolation_flag_only_in_worker_threads(monkeypatch):
    seen = []
    monkeypatch.setattr(
        rocket, "_get_subprocess_output", lambda args, **kw: seen.append(args)
    )
    rocket.get_subprocess_output(["python", "-m", "pip", "install", "x"])
    assert seen == [["python", "-m", "pip", "install", "x"]]


def test_settings_defaults(tmp_path):
    buildout = {"buildout": {"parts-directory": str(tmp_path / "parts")}}
    settings = rocket.Settings.from_buildout(buildout)
    assert settings.workers == (os.cpu_count() or 1)
    assert settings.build_isolation is False
    assert settings.cache is True
    assert settings.cache_directory == tmp_path / "parts" / ".rocket"
