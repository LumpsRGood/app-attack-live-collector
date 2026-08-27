from csv_parser import extract_appetizer_metrics, extract_appetizer_percent


def test_extract_appetizer_percent():
    text = """Items,ID,Quantity,Total,% of Sale
Beverages,51316,90,210.73,14.06
Appetizers,51369,6,75.24,5.02
Appetizer Sampler,1319971,1,13.99,
"""
    assert extract_appetizer_percent(text) == 5.02
    assert extract_appetizer_metrics(text) == {"count": 6, "percent": 5.02}


def test_missing_appetizers_means_zero_sold():
    text = """Items,ID,Quantity,Total,% of Sale
Beverages,51316,90,210.73,14.06
"""
    assert extract_appetizer_metrics(text) == {"count": 0, "percent": 0.0}
