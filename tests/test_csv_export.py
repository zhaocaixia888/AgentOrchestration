"""Tests for CSV formula escaping."""
import pytest
from src.common.csv_export import (
    needs_escaping,
    escape_field,
    escape_row,
    write_csv,
    escape_csv_data,
)


class TestNeedsEscaping:
    def test_formula_chars(self):
        """Test that formula-leading characters are detected."""
        for char in ["=", "+", "-", "@", "\t", "\r"]:
            actual = char if char != "\t" else "\t"
            # needs_escaping checks first char
            assert needs_escaping("=" + "A1"), f"Expected '=' to need escaping"
        assert not needs_escaping(""), "Empty string should not need escaping"
        assert not needs_escaping("normal"), "Normal text should not need escaping"
        assert not needs_escaping("123"), "Numeric text should not need escaping"
        assert not needs_escaping("= "), "Just '=' should be caught by first char"

    def test_safe_values(self):
        """Test values that don't need escaping."""
        safe_values = ["Hello", "12345", "2024-01-01", "abc123", "normal text"]
        for val in safe_values:
            assert not needs_escaping(val), f"'{val}' should not need escaping"


class TestEscapeField:
    def test_escaping(self):
        """Test that formula-leading values get escaped."""
        assert escape_field("=SUM(A1:A10)") == "'=SUM(A1:A10)"
        assert escape_field("+cmd") == "'+cmd"
        assert escape_field("-10") == "'-10"
        assert escape_field("@RISK") == "'@RISK"

    def test_no_escaping_needed(self):
        """Test that safe values pass through unchanged."""
        assert escape_field("Hello") == "Hello"
        assert escape_field("123") == "123"
        assert escape_field("") == ""
        assert escape_field("already safe") == "already safe"


class TestEscapeRow:
    def test_escapes_formula_row(self):
        """Test that formula-leading fields in a row are escaped."""
        row = ["Name", "=SUM(A1)", "+cmd", "safe"]
        expected = ["Name", "'=SUM(A1)", "'+cmd", "safe"]
        assert escape_row(row) == expected


class TestWriteCSV:
    def test_basic_csv_output(self):
        """Test basic CSV output with safe data."""
        rows = [["Alice", "30"], ["Bob", "25"]]
        headers = ["Name", "Age"]
        result = write_csv(rows, headers)
        assert "Name,Age" in result
        assert "Alice,30" in result
        assert "Bob,25" in result

    def test_formula_escaping_in_csv(self):
        """Test that formula characters are escaped in CSV output."""
        rows = [["=SUM(A1:A10)", "safe_value"]]
        result = write_csv(rows)
        assert "'=SUM(A1:A10)" in result
        assert "safe_value" in result

    def test_no_headers(self):
        """Test CSV output without headers."""
        rows = [["a", "b"], ["c", "d"]]
        result = write_csv(rows)
        assert result.count("\n") >= 1


class TestEscapeCSVData:
    def test_dict_to_csv(self):
        """Test converting dicts to CSV with escaping."""
        data = [
            {"name": "Alice", "score": "=100"},
            {"name": "Bob", "score": "95"},
        ]
        result = escape_csv_data(data, columns=["name", "score"])
        assert "Alice" in result
        assert "'=100" in result
        assert "Bob" in result

    def test_auto_columns(self):
        """Test auto-detection of columns from data."""
        data = [{"a": "1", "b": "2"}, {"a": "3", "b": "=4"}]
        result = escape_csv_data(data)
        assert "a,b" in result.split("\n")[0]
        assert "'=4" in result

    def test_empty_data(self):
        """Test empty data returns empty string."""
        assert escape_csv_data([]) == ""
