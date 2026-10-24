"""zc.buildout extension that speeds up the ``develop`` step.

zc.buildout 5+ runs ``pip install -e`` for every develop egg (for example
every mr.developer auto-checkout) on every run, one after the other, each in
a fresh isolated build environment.  This extension replaces that step:

* develop eggs are built in parallel (``rocket-workers``),
* builds run without pip build isolation, falling back to an isolated build
  per package if that fails (``rocket-build-isolation``),
* the generated files of every develop egg are cached and restored as long
  as its packaging metadata did not change (``rocket-cache``).
"""

import glob
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import zc.buildout.easy_install as easy_install

logger = logging.getLogger("nva.recipe.rocket")

#: Bump to invalidate all existing cache entries.
CACHE_FORMAT = "1"

#: Files whose content decides whether a develop egg must be rebuilt.
KEY_FILES = ("setup.py", "setup.cfg", "pyproject.toml")

#: Marker written into a cache entry once it is complete.
COMPLETE = ".rocket-complete"

_local = threading.local()
_get_subprocess_output = easy_install.get_subprocess_output


class NoIsolationFailed(Exception):
    """A build without build isolation failed; retry with isolation."""

    def __init__(self, output):
        super().__init__("build without isolation failed")
        self.output = output


def _bool(value):
    return value.strip().lower() in ("true", "yes", "on", "1")


@dataclass
class Settings:
    workers: int
    build_isolation: bool
    cache: bool
    cache_directory: Path

    @classmethod
    def from_buildout(cls, buildout):
        options = buildout["buildout"]
        workers = int(options.get("rocket-workers", "0") or 0)
        if workers < 1:
            workers = os.cpu_count() or 1
        cache_directory = options.get("rocket-cache-directory") or os.path.join(
            options["parts-directory"], ".rocket"
        )
        return cls(
            workers=workers,
            build_isolation=_bool(options.get("rocket-build-isolation", "false")),
            cache=_bool(options.get("rocket-cache", "true")),
            cache_directory=Path(cache_directory),
        )


def get_subprocess_output(args, **kw):
    """Wrap buildout's helper to build without isolation in worker threads.

    Only the develop workers set the thread-local flag, so regular egg
    installs in the main thread are left alone.  A failure is raised as
    :class:`NoIsolationFailed` without printing pip's output, because the
    build is retried with isolation.
    """
    if not getattr(_local, "no_isolation", False):
        return _get_subprocess_output(args, **kw)
    args = list(args)
    args.insert(args.index("install") + 1, "--no-build-isolation")
    result = subprocess.run(
        args, **kw, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    output = result.stdout.decode("utf-8", "replace")
    if result.returncode:
        raise NoIsolationFailed(output)
    return output


def _project_directory(setup):
    if not os.path.isdir(setup):
        setup = os.path.dirname(setup)
    return Path(setup).expanduser().resolve()


def _listing(directory):
    """Names that influence an editable install: top-level packages/modules.

    Covers the flat layout and the src layout two levels deep (namespace
    packages), plus the ``*.egg-info`` directory setuptools writes there.
    """
    names = []
    for base in (directory, directory / "src"):
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.name.startswith(".") or entry.name == "__pycache__":
                continue
            if entry.is_dir():
                names.append(str(entry.relative_to(directory)) + "/")
                if base.name == "src":
                    names.extend(
                        str(sub.relative_to(directory)) + "/"
                        for sub in sorted(entry.iterdir())
                        if sub.is_dir() and sub.name != "__pycache__"
                    )
            elif entry.suffix == ".py":
                names.append(str(entry.relative_to(directory)))
    return names


def _dist_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return ""


def cache_key(directory, settings):
    digest = hashlib.sha256()

    def add(*parts):
        for part in parts:
            digest.update(part if isinstance(part, bytes) else part.encode())
            digest.update(b"\0")

    add(CACHE_FORMAT, sys.executable, sys.version, str(directory))
    add(*(_dist_version(n) for n in ("zc.buildout", "setuptools", "pip")))
    add(str(settings.build_isolation))
    for name in KEY_FILES:
        path = directory / name
        add(name, path.read_bytes() if path.is_file() else b"-")
    add(*_listing(directory))
    return digest.hexdigest()


def _entry_prefix(directory):
    path_hash = hashlib.sha256(str(directory).encode()).hexdigest()[:8]
    return f"{directory.name}-{path_hash}-"


def _copy_into(source, dest):
    """Copy the generated files from ``source`` into ``dest``."""
    copied = []
    for entry in sorted(os.listdir(source)):
        if entry.startswith(".rocket"):
            continue
        src, target = os.path.join(source, entry), os.path.join(dest, entry)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        elif os.path.lexists(target):
            os.remove(target)
        if os.path.isdir(src):
            shutil.copytree(src, target, symlinks=True)
        else:
            shutil.copy2(src, target)
        copied.append(target)
    return copied


def _build(setup, staging, settings):
    if not settings.build_isolation:
        _local.no_isolation = True
        try:
            easy_install.develop(setup, str(staging))
            return
        except NoIsolationFailed as error:
            logger.info(
                "Building %r without build isolation failed, "
                "retrying with isolation.",
                setup,
            )
            logger.debug(error.output)
            for entry in staging.iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        finally:
            _local.no_isolation = False
    easy_install.develop(setup, str(staging))


def develop_one(setup, dest, settings):
    """Develop one egg into ``dest``; return True if it came from the cache."""
    directory = _project_directory(setup)
    prefix = _entry_prefix(directory)
    cache_directory = settings.cache_directory
    cache_directory.mkdir(parents=True, exist_ok=True)

    if settings.cache:
        entry = cache_directory / (prefix + cache_key(directory, settings)[:16])
        if (entry / COMPLETE).is_file():
            _copy_into(entry, dest)
            return True

    staging = Path(tempfile.mkdtemp(prefix=".tmp-" + prefix, dir=cache_directory))
    try:
        _build(setup, staging, settings)
        _copy_into(staging, dest)
        if settings.cache:
            # Key again: the build may have created the *.egg-info directory.
            entry = cache_directory / (prefix + cache_key(directory, settings)[:16])
            for old in cache_directory.glob(prefix + "*"):
                shutil.rmtree(old, ignore_errors=True)
            (staging / COMPLETE).write_text(str(directory) + "\n")
            staging.rename(entry)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return False


def _develop(self, settings):
    """Replacement for ``zc.buildout.buildout.Buildout._develop``."""
    __doing__ = "Processing directories listed in the develop option"  # noqa

    develop = self["buildout"].get("develop")
    if not develop:
        return ""

    dest = self["buildout"]["develop-eggs-directory"]
    old_files = os.listdir(dest)

    setups = []
    for setup in develop.split():
        setup = self._buildout_path(setup)
        files = glob.glob(setup)
        if not files:
            self._logger.warning("Couldn't develop %r (not found)", setup)
        setups.extend(sorted(files))

    here = os.getcwd()
    started = time.monotonic()
    cached = 0
    pool = ThreadPoolExecutor(max_workers=settings.workers)
    try:
        try:
            futures = [
                (setup, pool.submit(develop_one, setup, dest, settings))
                for setup in setups
            ]
            for setup, future in futures:
                if future.result():
                    cached += 1
                    self._logger.info("Develop: %r (cached)", setup)
                else:
                    self._logger.info("Develop: %r", setup)
            pool.shutdown()
        except Exception:
            pool.shutdown(cancel_futures=True)
            # Roll back, like buildout does: remove any files we created.
            self._sanity_check_develop_eggs_files(dest, old_files)
            self._uninstall(
                "\n".join(
                    os.path.join(dest, f)
                    for f in os.listdir(dest)
                    if f not in old_files
                )
            )
            raise

        self._sanity_check_develop_eggs_files(dest, old_files)
        logger.info(
            "Developed %d eggs in %.1fs (%d built, %d cached, %d workers).",
            len(setups),
            time.monotonic() - started,
            len(setups) - cached,
            cached,
            settings.workers,
        )
        return "\n".join(
            sorted(os.path.join(dest, f) for f in os.listdir(dest) if f not in old_files)
        )
    finally:
        os.chdir(here)


def extension(buildout):
    """Entry point for ``extensions = nva.recipe.rocket``."""
    if not hasattr(easy_install, "call_pip_install"):
        logger.warning(
            "nva.recipe.rocket needs zc.buildout 5 or newer; not activated."
        )
        return
    settings = Settings.from_buildout(buildout)
    easy_install.get_subprocess_output = get_subprocess_output
    buildout._develop = types.MethodType(
        lambda self: _develop(self, settings), buildout
    )
