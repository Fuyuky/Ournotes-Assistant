"""Explicit program resources and writable user data; never depend on cwd."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("OURNOTES_DATA_ROOT", ROOT)).resolve()


@dataclass(frozen=True)
class ProjectPaths:
    data_root: Path = DATA_ROOT
    resource_root: Path = ROOT

    @property
    def state(self):
        return self.data_root / "state"

    @property
    def archives(self):
        return self.data_root / "results/archive/accounts"

    @property
    def catalog(self):
        return self.data_root / "data/catalog/site"

    @property
    def snapshots(self):
        return self.data_root / "data/catalog/snapshots"

    @property
    def web(self):
        return self.resource_root / "web"


PATHS = ProjectPaths()
STATE = PATHS.state
ASSETS = ROOT / "assets"
CATALOG = PATHS.catalog
REFERENCE = ASSETS / "reference"
