# Changelog

## 0.1.3 (unreleased)

- Nothing changed yet.

## 0.1.2 (2026-11-14)

- Fix: a fresh checkout built without build isolation had no `*.egg-info`
  in its source directory, so buildout did not recognise the develop egg and
  installed a release from the index instead. The metadata of setuptools
  projects is now written separately; if that does not work either, the
  package is rebuilt with isolation.

## 0.1.1 (2026-11-12)

- The cache notices changes in git checkouts: a develop egg is rebuilt when
  its commit or its uncommitted changes differ from the cached build.

## 0.1.0 (2026-10-24)

- Initial release: parallel develop eggs, builds without build isolation
  with per-package fallback, and a cache for unchanged develop eggs.
