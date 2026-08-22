#This keeps the BIM models which are large and live running well inside the codes

import os
from pathlib import Path

CORE_BACKEND = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_BACKEND.parent
REPO_ROOT = PROJECT_ROOT.parent

CANDIDATE_DIRS = [
    PROJECT_ROOT / "sample_models",
    REPO_ROOT / "sample_models",
    REPO_ROOT.parent / "bim residential models",
    REPO_ROOT.parent / "testing IFC data for project",
]


def default_ifc():
    override = os.getenv("EVAC_SAMPLE_IFC")
    if override and Path(override).is_file():
        return override
    for directory in CANDIDATE_DIRS:
        if directory.is_dir():
            for ifc in sorted(directory.glob("*.ifc")):
                return str(ifc)
    return None

#Resolves IFC input
def resolve_ifc(argv):
    if len(argv) > 1:
        return argv[1]
    path = default_ifc()
    if path is None:
        raise SystemExit(
            "No IFC file given and no sample model found. Pass a path as the first "
            "argument, or set EVAC_SAMPLE_IFC to a .ifc file."
        )
    return path
