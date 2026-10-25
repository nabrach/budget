"""Settings read from environment variables, with local-only defaults."""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    data_dir: Path
    host: str
    port: int
    open_browser: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "budget.db"


def load_config(env=os.environ) -> Config:
    return Config(
        data_dir=Path(env.get("BUDGET_DATA_DIR") or ROOT / "data"),
        host=env.get("HOST", "127.0.0.1"),
        port=int(env.get("PORT", "8000")),
        # Set BUDGET_NO_BROWSER=1 when running headless (e.g. in Docker).
        open_browser=not env.get("BUDGET_NO_BROWSER"),
    )
