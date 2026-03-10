"""
parsers — Multi-Language Parser Backends

Pluggable language parsers for code indexing. Each parser extracts
CodeUnits (functions, classes, routes) from source files.

Supported languages:
    - Python (AST-based, existing)
    - JavaScript/TypeScript (regex-based, new)
    - Go (regex-based, new)
    - Java (regex-based, new)
"""

from .base import LanguageParser, get_parser_for_file, SUPPORTED_EXTENSIONS
from .javascript_parser import JavaScriptParser
from .go_parser import GoParser
from .java_parser import JavaParser

__all__ = [
    "LanguageParser",
    "get_parser_for_file",
    "SUPPORTED_EXTENSIONS",
    "JavaScriptParser",
    "GoParser",
    "JavaParser",
]
