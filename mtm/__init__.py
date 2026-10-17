"""uTest Machinery — standalone control CLI.

This package implements the PR 1 "project bootstrap and control" milestone:
bench initialisation, scope selection, environment diagnosis, image check and
headless workflow orchestration. Test generation, offline verification and
reporting arrive in later milestones and are declared here but not implemented.
"""

__version__ = "0.1.0"

#: Product milestone implemented by this build. Used in diagnostics so a user
#: can tell which capabilities a given installation actually provides.
MILESTONE = "PR1-bootstrap-control"

#: Bench/project config schema version understood by this build.
SCHEMA_VERSION = 1
