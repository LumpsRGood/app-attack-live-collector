import csv
import io


def extract_appetizer_percent(csv_text: str) -> float:
    rows = csv.reader(io.StringIO(csv_text))
    for row in rows:
        if row and row[0].strip().casefold() == "appetizers":
            if len(row) < 5 or not row[4].strip():
                raise ValueError("Appetizers row has no % of Sale value.")
            return float(row[4])
    raise ValueError("Appetizers category was not found in the Menu Mix export.")
