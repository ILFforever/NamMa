import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://namma:namma@localhost:5432/namma")
RAW_DIR = Path(os.environ.get("RAW_DIR", ROOT / "var" / "raw"))
