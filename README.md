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
  package is retried with isolation.
- **Cache** — the files a develop egg generates in `develop-eggs/` are cached.
  On the next run they are copied back instead of rebuilt, as long as the
  package's `setup.py`, `setup.cfg`, `pyproject.toml` and its top-level
  package layout are unchanged. Code changes never need a rebuild — editable
  installs point at the source directory.

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

## When is a develop egg rebuilt?

The cache key of a develop egg is made from:

- the contents of `setup.py`, `setup.cfg` and `pyproject.toml`,
- the names of the top-level packages and modules (flat layout and src
  layout, two levels deep for namespace packages) and the `*.egg-info`
  directory,
- the Python interpreter, the zc.buildout, setuptools and pip versions,
- the `rocket-build-isolation` setting.

Anything else that affects the generated metadata is **not** seen, for
example a version computed from git tags (setuptools-scm). Run once with
`buildout:rocket-cache=false`, or delete `parts/.rocket`, to force a rebuild.

## Other things that make buildout slow

Not handled by this extension, but worth checking in the buildout output:

- `Scanning index of all packages (this may take a while)` — an unpinned
  package was not found on its index page, so setuptools downloads and parses
  the whole PyPI `/simple/` root page (~1 million links), once per part. Pin
  the package listed in `Couldn't retrieve index page for ...`.
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
