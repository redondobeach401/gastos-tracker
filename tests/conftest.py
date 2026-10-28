"""fetch.py lee sus variables de entorno al importarse: ponemos valores falsos antes de los tests."""
import os
import sys
from pathlib import Path

for name, value in {
    "EB_APP_ID": "test",
    "EB_PRIVATE_KEY": "test",
    "EB_SESSIONS": "{}",
    "CUTOFF_DATE": "2026-10-01",
    "SUPABASE_DB_URL": "postgresql://test",
    "SUPABASE_CA_CERT": "test",
}.items():
    os.environ.setdefault(name, value)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
