"""
parsers — Multi-Language Parser Backends

Pluggable language parsers for code indexing. Each parser extracts
CodeUnits (functions, classes, routes) from source files.

Supported languages:
    - Python (AST-based, existing)
    - JavaScript/TypeScript (tree-sitter or regex fallback)
    - Go (tree-sitter or regex fallback)
    - Java (tree-sitter or regex fallback)
"""

from .base import LanguageParser, get_parser_for_file, SUPPORTED_EXTENSIONS

# Import regex-based parsers first (they become fallbacks)
from .javascript_parser import JavaScriptParser
from .go_parser import GoParser
from .java_parser import JavaParser

# Import tree-sitter parsers second (they override extensions if available)
try:
    from .ts_javascript_parser import TreeSitterJavaScriptParser
except ImportError:
    pass
try:
    from .ts_go_parser import TreeSitterGoParser
except ImportError:
    pass
try:
    from .ts_java_parser import TreeSitterJavaParser
except ImportError:
    pass

__all__ = [
    "LanguageParser",
    "get_parser_for_file",
    "SUPPORTED_EXTENSIONS",
    "JavaScriptParser",
    "GoParser",
    "JavaParser",
]
