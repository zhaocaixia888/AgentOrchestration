"""CSV export utilities with spreadsheet formula escaping."""
import csv
import io
from typing import List, Dict, Any

# Characters that trigger spreadsheet formula interpretation
FORMULA_CHARS = frozenset({'=', '+', '-', '@', '	', ''})

def needs_escaping(value: str) -> bool:
    """Check if a value starts with a spreadsheet formula character."""
    return bool(value) and value[0] in FORMULA_CHARS

def escape_field(value: str) -> str:
    """Escape a single CSV field to prevent formula injection.
    
    Prepends a single quote to values starting with formula characters,
    which tells spreadsheet software to treat the value as text.
    """
    if needs_escaping(value):
        return "'" + value
    return value

def escape_row(row: List[str]) -> List[str]:
    """Escape all fields in a row."""
    return [escape_field(field) for field in row]

def write_csv(rows: List[List[str]], headers: List[str] = None) -> str:
    """Write rows to CSV with formula escaping.
    
    Args:
        rows: Data rows (each row is a list of strings)
        headers: Optional header row
    
    Returns:
        CSV string with formula-safe escaping
    """
    output = io.StringIO()
    writer = csv.writer(output)
    
    if headers:
        writer.writerow(escape_row(headers))
    
    for row in rows:
        writer.writerow(escape_row(row))
    
    return output.getvalue()

def escape_csv_data(data: List[Dict[str, Any]], columns: List[str] = None) -> str:
    """Convert dict records to CSV with formula escaping.
    
    Args:
        data: List of dictionaries
        columns: Column order (defaults to dict keys from first row)
    
    Returns:
        CSV string with formula-safe escaping
    """
    if not data:
        return ""
    
    if not columns:
        columns = list(data[0].keys())
    
    headers = [str(h) for h in columns]
    rows = [[str(row.get(col, "")) for col in columns] for row in data]
    
    return write_csv(rows, headers)
