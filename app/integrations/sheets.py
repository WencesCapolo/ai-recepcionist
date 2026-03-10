import base64
import json
import logging
from functools import lru_cache
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


@lru_cache
def _get_gspread_client() -> gspread.Client:
    """Authenticate once, reuse the client. Cached for the lifetime of the process."""
    raw = base64.b64decode(settings.google_service_account_json).decode("utf-8")
    service_account_info = json.loads(raw)
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return gspread.authorize(creds)


class SheetsClient:
    """
    Thin wrapper around gspread for reading product data.
    All methods return plain dicts/lists — no gspread types leak out.
    """

    def get_all_rows(self, sheet_id: str, worksheet: str = "productos") -> list[dict]:
        """Returns all rows as a list of dicts keyed by header row."""
        try:
            gc = _get_gspread_client()
            sheet = gc.open_by_key(sheet_id).worksheet(worksheet)
            return sheet.get_all_records()
        except Exception as e:
            logger.error(f"Sheets read failed [sheet={sheet_id}]: {e}")
            return []

    def find_product(
        self, sheet_id: str, product_name: str, worksheet: str = "productos"
    ) -> Optional[dict]:
        """
        Case-insensitive search for a product by name.
        Returns the first matching row or None.
        """
        rows = self.get_all_rows(sheet_id, worksheet)
        search = product_name.lower().strip()

        # Pass 1: exact substring ("tornillo 6x50" in "tornillo 6x50")
        for row in rows:
            if search in str(row.get("producto", "")).lower():
                return row

        # Pass 2: all search words appear in product name
        # handles "cable 2.5mm" → "Cable unipolar 2.5mm"
        words = [w for w in search.split() if len(w) > 2]
        if words:
            for row in rows:
                product_lower = str(row.get("producto", "")).lower()
                if all(w in product_lower for w in words):
                    return row

        # Pass 3: stem match — strip trailing s/es for Spanish plurals
        # handles "tornillos" → "tornillo", "candados" → "candado"
        stemmed = search.rstrip("s").rstrip("e") if search.endswith(("s", "es")) else search
        if stemmed != search:
            for row in rows:
                product_lower = str(row.get("producto", "")).lower()
                if stemmed in product_lower:
                    return row

        return None


@lru_cache
def get_sheets_client() -> SheetsClient:
    return SheetsClient()

