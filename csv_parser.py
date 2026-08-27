import csv
import io


def extract_appetizer_metrics(csv_text: str) -> dict[str, float | int]:
    rows = list(csv.reader(io.StringIO(csv_text)))
    has_menu_mix_header = any(
        len(row) >= 5
        and row[0].strip().casefold() == "items"
        and row[2].strip().casefold() == "quantity"
        and row[4].strip().casefold() == "% of sale"
        for row in rows
    )
    if not has_menu_mix_header:
        raise ValueError("Menu Mix export columns were not found.")

    for row in rows:
        if row and row[0].strip().casefold() == "appetizers":
            if len(row) < 5 or not row[4].strip():
                raise ValueError("Appetizers row has no % of Sale value.")
            quantity = int(float(row[2])) if len(row) > 2 and row[2].strip() else 0
            return {"count": quantity, "percent": float(row[4])}

    # A valid Menu Mix export without an Appetizers category means none were sold.
    return {"count": 0, "percent": 0.0}


def extract_appetizer_percent(csv_text: str) -> float:
    return float(extract_appetizer_metrics(csv_text)["percent"])
