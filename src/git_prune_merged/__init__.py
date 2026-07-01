"""Prune gone remotes and delete only the local branches merged to main."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("git-prune-merged")
except PackageNotFoundError:  # not installed (e.g. running from a source checkout)
    __version__ = "0.0.0+unknown"
