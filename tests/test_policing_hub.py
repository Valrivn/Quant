import pytest
from opencode_scripts.policing_hub import verify_local_ast

def test_ast_flags_malformed_syntax():
    broken_python = "def scrape_data()\n    print('missing colon')"
    assert verify_local_ast(broken_python) is False

def test_ast_passes_clean_syntax():
    clean_python = "def scrape_data():\n    print('has colon')"
    assert verify_local_ast(clean_python) is True
