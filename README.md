# nva.recipe.rocket

[![tests](https://github.com/novareto/nva.recipe.rocket/actions/workflows/tests.yml/badge.svg)](https://github.com/novareto/nva.recipe.rocket/actions/workflows/tests.yml)

A [zc.buildout](https://github.com/buildout/buildout) extension that makes the
**develop step** fast.

Since zc.buildout 5, every develop egg — every `develop =` entry and every
[mr.developer](https://pypi.org/project/mr.developer/) auto-checkout — is
installed with `pip install -e` on **every** buildout run: one after the
other, each in a fresh isolated build environment (pip installs setuptools
into a temporary venv first). With 20+ checkouts that alone takes 40+ seconds,
even when nothing changed.

`nva.recipe.rocket` replaces that step:

- **Parallel** — develop eggs are built concurrently, one worker per CPU.
- **No build isolation** — `pip install -e --no-build-isolation` uses the
  setuptools buildout already runs with. If that fails for a package (for
  example its build backend, like hatchling, is not installed), only that
  package is retried with isolation. pip skips writing the `*.egg-info`
  directory in this mode, which buildout needs to recognise the develop egg;
  for setuptools projects it is generated in a separate, cheap step.
- **Cache** — the files a develop egg generates in `develop-eggs/` are cached.
  On the next run they are copied back instead of rebuilt, as long as the
  package's `setup.py`, `setup.cfg`, `pyproject.toml` and its top-level
  package layout are unchanged. A git checkout is also rebuilt when its
  commit or its uncommitted changes differ from the cached build.

Measured on a project with 22 mr.developer checkouts:

| develop step                          | time  |
|---------------------------------------|-------|
| zc.buildout 6, plain                  | ~44 s |
| without build isolation               | ~18 s |
| + parallel (10 workers)               | ~5 s  |
| + cache, nothing changed              | < 1 s |

## Usage

```ini
[buildout]
extensions =
    mr.developer
    nva.recipe.rocket
```

The order of extensions does not matter. Requires zc.buildout 5 or newer.

## Options

All options go into the `[buildout]` section and can be set on the command
line, e.g. `bin/buildout buildout:rocket-cache=false`.

| option                    | default               | meaning |
|---------------------------|-----------------------|---------|
| `rocket-workers`          | number of CPUs        | parallel builds; `1` builds sequentially |
| `rocket-build-isolation`  | `false`               | `true` builds with pip build isolation, like plain buildout |
| `rocket-cache`            | `true`                | reuse the files of unchanged develop eggs |
| `rocket-cache-directory`  | `${buildout:parts-directory}/.rocket` | where the cache lives |
| `rocket-scan-index`       | `true`                | `false` never downloads the list of all packages of the index, see below |

## When is a develop egg rebuilt?

The cache key of a develop egg is made from:

- the contents of `setup.py`, `setup.cfg` and `pyproject.toml`,
- the names of the top-level packages and modules (flat layout and src
  layout, two levels deep for namespace packages) and the `*.egg-info`
  directory,
- for a git checkout: the checked out commit and its uncommitted changes
  (modified, staged and untracked files that are not ignored, with their
  size and modification time),
- the Python interpreter, the zc.buildout, setuptools and pip versions,
- the `rocket-build-isolation` setting.

So a checkout is rebuilt after a `git pull`, a branch switch or a local
edit, and only that checkout. Outside of git, or for things git does not
show (ignored files, a new tag on the same commit for setuptools-scm),
changes that affect the generated metadata are **not** seen. Run once with
`buildout:rocket-cache=false`, or delete `parts/.rocket`, to force a rebuild.

## Other things that make buildout slow

Worth checking in the buildout output:

- `Scanning index of all packages (this may take a while)` — a package was
  not found on its index page, so buildout downloads and parses the whole
  PyPI `/simple/` root page (~1 million links), once per part. This hits
  packages the index does not have, like internal packages that come from
  `find-links`. Set `rocket-scan-index = false` to skip that scan: the page
  of a package is requested under its normalised name anyway, so the scan
  finds nothing new on PyPI or any other PEP 503 index. Leave it at `true`
  if your `index` is a plain directory listing instead. Without the option,
  pin the package listed in `Couldn't retrieve index page for ...`, or run
  with `newest = false`, so buildout does not ask the index for it.
- `Upgraded: setuptools version ...; Restarting.` on every run — the
  setuptools pinned in `[versions]` differs from the one installed in the
  virtualenv that runs buildout. Make them match.
- `PIP_NO_BUILD_ISOLATION=0` also speeds up plain buildout (yes, `0`: pip's
  environment variable is inverted for historical reasons).

## Development

```sh
uv sync
uv run pytest
```
