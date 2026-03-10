import base64
import json
import logging
from functools import lru_cache
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import process, fuzz

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
        Fuzzy search for a product by name.
        Returns the best matching row above 70% similarity, or None.
        """
        rows = self.get_all_rows(sheet_id, worksheet)
        if not rows:
            return None

        names = [str(r.get("producto", "")) for r in rows]
        match = process.extractOne(
            product_name,
            names,
            scorer=fuzz.partial_ratio,
            score_cutoff=70,
        )
        if not match:
            return None

        matched_name = match[0]
        for i, name in enumerate(names):
            if name == matched_name:
                return rows[i]
        return None

@lru_cache
def get_sheets_client() -> SheetsClient:
    return SheetsClient()

