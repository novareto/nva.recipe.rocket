import os
import subprocess
import textwrap

import pkg_resources
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


def git(directory, *args):
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=directory,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def checkout(project, develop_calls):
    """``pkga`` is a git checkout that was developed (and cached) once."""
    pkga = project / "pkga"
    (pkga / ".gitignore").write_text("*.egg-info\nbuild\n__pycache__\n")
    (pkga / "version.txt").write_text("1.0\n")
    git(pkga, "init", "-q")
    git(pkga, "add", ".")
    git(pkga, "commit", "-q", "-m", "initial")
    run_buildout(project)
    develop_calls.clear()
    return pkga


def test_clean_checkout_is_cached(project, checkout, develop_calls):
    run_buildout(project)
    assert develop_calls == []


def test_uncommitted_change_rebuilds_only_that_checkout(
    project, checkout, develop_calls
):
    (checkout / "version.txt").write_text("1.1.dev0\n")
    run_buildout(project)
    assert develop_calls == ["pkga"]

    # The same uncommitted change is cached ...
    develop_calls.clear()
    run_buildout(project)
    assert develop_calls == []

    # ... until the file is edited again.
    (checkout / "version.txt").write_text("1.1.dev0+local\n")
    run_buildout(project)
    assert develop_calls == ["pkga"]


def test_untracked_file_rebuilds(project, checkout, develop_calls):
    (checkout / "MANIFEST.in").write_text("include version.txt\n")
    run_buildout(project)
    assert develop_calls == ["pkga"]


def test_new_commit_rebuilds(project, checkout, develop_calls):
    git(checkout, "commit", "-q", "--allow-empty", "-m", "second")
    run_buildout(project)
    assert develop_calls == ["pkga"]


def test_git_state_outside_of_a_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert rocket._git_state(tmp_path) == []


def test_cache_can_be_disabled(project, develop_calls):
    run_buildout(project, "buildout:rocket-cache=false")
    run_buildout(project, "buildout:rocket-cache=false")
    assert sorted(develop_calls) == ["pkga", "pkga", "pkgb", "pkgb"]


def test_sequential_worker(project, develop_calls):
    run_buildout(project, "buildout:rocket-workers=1")
    assert sorted(develop_calls) == ["pkga", "pkgb"]


def test_fresh_checkout_gets_metadata(project):
    """buildout only recognises a develop egg if its egg-link points at a
    directory with metadata; a build without isolation does not write any."""
    assert not list(project.glob("pkg*/**/*.egg-info"))
    run_buildout(project)
    assert (project / "pkga" / "pkga.egg-info" / "PKG-INFO").is_file()
    assert (project / "pkgb" / "src" / "pkgb.egg-info" / "PKG-INFO").is_file()


def test_no_metadata_falls_back_to_build_isolation(project, monkeypatch):
    attempts = []
    original = easy_install.develop

    def develop(setup, dest, *args, **kw):
        attempts.append(not getattr(rocket._local, "no_isolation", False))
        rocket._local.no_isolation = True  # we are offline
        return original(setup, dest, *args, **kw)

    monkeypatch.setattr(easy_install, "develop", develop)
    monkeypatch.setattr(rocket, "_write_metadata", lambda directory: None)
    (project / "buildout.cfg").write_text(
        "[buildout]\ndevelop = pkga\nparts =\noffline = true\nnewest = false\n"
    )
    run_buildout(project, "buildout:rocket-workers=1")
    assert attempts == [False, True]


def test_other_build_backends_are_left_alone(project):
    (project / "pkga" / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    )
    assert not rocket._uses_setuptools(project / "pkga")
    assert rocket._uses_setuptools(project / "pkgb")


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
    assert settings.scan_index is True


@pytest.fixture
def scanned_urls(tmp_path, monkeypatch):
    """Look up a package the index does not have; return the URLs scanned."""
    index = easy_install.AllowHostsPackageIndex
    # Registered so that the patch of the extension is undone after the test.
    monkeypatch.setattr(index, "scan_all", index.scan_all)
    (tmp_path / "index").mkdir()
    (tmp_path / "buildout.cfg").write_text("[buildout]\nparts =\n")
    index_url = (tmp_path / "index").as_uri() + "/"

    def lookup(*options):
        cloptions = [("buildout", *o.split("=", 1)) for o in options]
        buildout = zc.buildout.buildout.Buildout(
            str(tmp_path / "buildout.cfg"), cloptions, use_user_defaults=False
        )
        rocket.extension(buildout)
        urls = []
        package_index = index(index_url)
        monkeypatch.setattr(package_index, "scan_url", urls.append)
        package_index.find_packages(pkg_resources.Requirement.parse("missing"))
        return [url.removeprefix(index_url) for url in urls]

    return lookup


def test_index_is_scanned_by_default(scanned_urls):
    assert scanned_urls() == ["missing/", ""]


def test_index_scan_can_be_disabled(scanned_urls):
    assert scanned_urls("rocket-scan-index=false") == ["missing/"]
