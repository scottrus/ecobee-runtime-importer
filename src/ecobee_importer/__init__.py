"""Import ecobee runtimeReport history into VictoriaMetrics."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Resolved from installed package metadata, which setuptools-scm derives
    # from the git tag at build time. No version literal is committed anywhere.
    __version__ = version("ecobee-runtime-importer")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"
