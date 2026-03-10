"""
base.py — Language Parser Base Class & Registry

All language parsers inherit from LanguageParser and implement parse().
The registry auto-selects the right parser based on file extension.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Type

from inferra.indexer import CodeUnit


class LanguageParser(ABC):
    """Base class for language-specific parsers."""

    # Subclasses must set these
    LANGUAGE: str = ""
    EXTENSIONS: List[str] = []

    @abstractmethod
    def parse(
        self,
        source: str,
        filepath: str,
        module_name: str,
    ) -> List[CodeUnit]:
        """
        Parse source code and return a list of CodeUnits.

        Args:
            source: Raw source code string
            filepath: Absolute path to the file
            module_name: Dotted module name (e.g. "app.routes.users")

        Returns:
            List of CodeUnit objects (functions, classes, routes, etc.)
        """
        ...

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text for TF-IDF search (shared across parsers)."""
        import re
        tokens = re.findall(r"[a-zA-Z_]\w{2,}", text.lower())
        stop = {
            "self", "none", "true", "false", "return", "import", "from",
            "def", "class", "async", "await", "function", "const", "let",
            "var", "func", "public", "private", "protected", "static",
            "void", "string", "int", "bool", "float", "the", "and",
            "for", "not", "with", "this", "that",
        }
        return [t for t in tokens if t not in stop]

    def _extract_log_patterns(self, body: str) -> List[str]:
        """Extract logging/print patterns from source (shared)."""
        import re
        patterns = []
        for match in re.finditer(
            r"""(?:log(?:ger)?\.(?:info|warn|error|debug|warning|critical|fatal)|print|console\.(?:log|error|warn|info)|fmt\.(?:Print|Printf|Println|Errorf)|log\.(?:Print|Printf|Println|Fatal))\s*\(\s*[\"'`](.*?)[\"'`]""",
            body,
        ):
            patterns.append(match.group(1))
        return patterns


# ── Parser Registry ──────────────────────────────────────────────────────────

_PARSERS: Dict[str, Type[LanguageParser]] = {}

SUPPORTED_EXTENSIONS: Dict[str, str] = {}  # ext → language


def register_parser(parser_class: Type[LanguageParser]):
    """Register a parser class for its declared extensions."""
    for ext in parser_class.EXTENSIONS:
        _PARSERS[ext] = parser_class
        SUPPORTED_EXTENSIONS[ext] = parser_class.LANGUAGE
    return parser_class


def get_parser_for_file(filepath: str) -> Optional[LanguageParser]:
    """Get the appropriate parser instance for a file based on extension."""
    ext = Path(filepath).suffix.lower()
    parser_class = _PARSERS.get(ext)
    if parser_class:
        return parser_class()
    return None
