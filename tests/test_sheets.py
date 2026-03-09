import sys
sys.path.insert(0, ".")
from app.integrations.sheets import _get_gspread_client

try:
    gc = _get_gspread_client()
    sheet = gc.open_by_key("1LPPx8pe250W4qVWxR_ROGBHapAy-PS0BMr8iQ-M43oo").worksheet("productos")
    print(sheet.get_all_records())
except Exception as e:
    import traceback
    traceback.print_exc()
