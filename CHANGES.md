# Changelog

## 0.1.1 (2026-11-12)

- The cache notices changes in git checkouts: a develop egg is rebuilt when
  its commit or its uncommitted changes differ from the cached build.

## 0.1.0 (2026-10-24)

- Initial release: parallel develop eggs, builds without build isolation
  with per-package fallback, and a cache for unchanged develop eggs.
