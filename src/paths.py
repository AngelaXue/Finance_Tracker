"""Every file location the app uses, in one place. All paths are relative
to the project folder, so the whole folder can be copied to another
machine and just work.

Demo mode (`python app.py --demo`, which sets FINANCE_TRACKER_DEMO=1)
swaps in a separate database, classifier and statements folder under
data/, so exploring with fake data never touches real data.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"

DEMO = os.environ.get("FINANCE_TRACKER_DEMO") == "1"

REAL_DB_PATH = DATA_DIR / "finances.db"
DEMO_DB_PATH = DATA_DIR / "demo.db"
DB_PATH = DEMO_DB_PATH if DEMO else REAL_DB_PATH

# The trained categorizer is derived from your own descriptions, so it's
# private data too (gitignored with the rest of data/).
MODEL_PATH = DATA_DIR / ("demo_classifier.joblib" if DEMO else "classifier.joblib")

# Bank CSV statements, one subfolder per account (see csv_importer.py)
INCOMING_DIR = (DATA_DIR / "demo_incoming") if DEMO else (BASE_DIR / "incoming_statements")

# Optional one-time import of older history (see history_importer.py)
HISTORY_DIR = (DATA_DIR / "demo_history") if DEMO else (BASE_DIR / "import_history")

# Optional user-defined CSV layouts (see src/parsers/generic.py)
CSV_FORMATS_PATH = CONFIG_DIR / "csv_formats.json"
