"""Repo-relative resolution of sample IFC models.

Replaces the hardcoded personal-desktop paths the module ``__main__`` blocks used to carry.
The BIM models are large and live outside the repo, so this does a best-effort lookup so the
demos keep working on a dev machine without committing an absolute path.

Resolution order for the default model:
  1. ``$EVAC_SAMPLE_IFC`` (explicit override)
  2. the first ``*.ifc`` found under a set of candidate directories, searched in order

Callers should still accept an explicit path via ``sys.argv[1]`` and fall back to
``default_ifc()`` / ``resolve_ifc(sys.argv)``.
"""

import os
from pathlib import Path

# core_backend/ -> evacuation_project/ (import + run root) -> AI_Msc_project/ (git repo root)
CORE_BACKEND = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_BACKEND.parent
REPO_ROOT = PROJECT_ROOT.parent

# Candidate directories to search for a sample .ifc, in priority order.
CANDIDATE_DIRS = [
    PROJECT_ROOT / "sample_models",
    REPO_ROOT / "sample_models",
    REPO_ROOT.parent / "bim residential models",
    REPO_ROOT.parent / "testing IFC data for project",
]


def default_ifc():
    """Return a path to a sample IFC as a string, or None if none can be located."""
    override = os.getenv("EVAC_SAMPLE_IFC")
    if override and Path(override).is_file():
        return override
    for directory in CANDIDATE_DIRS:
        if directory.is_dir():
            for ifc in sorted(directory.glob("*.ifc")):
                return str(ifc)
    return None


def resolve_ifc(argv):
    """Resolve an IFC path from CLI args, falling back to ``default_ifc()``.

    Usage in a module ``__main__``::

        from core_backend.sample_paths import resolve_ifc
        ifc_path = resolve_ifc(sys.argv)
    """
    if len(argv) > 1:
        return argv[1]
    path = default_ifc()
    if path is None:
        raise SystemExit(
            "No IFC file given and no sample model found. Pass a path as the first "
            "argument, or set EVAC_SAMPLE_IFC to a .ifc file."
        )
    return path
