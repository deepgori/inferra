"""
base.py — Language Parser Base Class & Registry

All language parsers inherit from LanguageParser and implement parse().
The registry auto-selects the right parser based on file extension.
Tree-sitter parsers take priority over regex-based parsers when available.
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

    def parse_file(self, filepath: str, content: str) -> List[CodeUnit]:
        """Alternative parse interface used by tree-sitter parsers.

        Default: delegates to parse(). Tree-sitter parsers override this.
        """
        module_name = Path(filepath).stem
        return self.parse(content, filepath, module_name)

    def can_parse(self) -> bool:
        """Return True if this parser can actually parse files.

        Tree-sitter parsers return False if tree-sitter is not installed,
        allowing the registry to fall back to regex parsers.
        """
        return True

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
_FALLBACK_PARSERS: Dict[str, Type[LanguageParser]] = {}

SUPPORTED_EXTENSIONS: Dict[str, str] = {}  # ext → language


def register_parser(parser_class: Type[LanguageParser]):
    """Register a parser class for its declared extensions.

    If a parser is already registered for an extension, the new parser
    takes priority and the old one becomes the fallback. This lets
    tree-sitter parsers override regex parsers when imported later.
    """
    for ext in parser_class.EXTENSIONS:
        existing = _PARSERS.get(ext)
        if existing:
            _FALLBACK_PARSERS[ext] = existing
        _PARSERS[ext] = parser_class
        SUPPORTED_EXTENSIONS[ext] = parser_class.LANGUAGE
    return parser_class


def get_parser_for_file(filepath: str) -> Optional[LanguageParser]:
    """Get the appropriate parser instance for a file based on extension.

    Returns the tree-sitter parser if available and can_parse() returns True,
    otherwise falls back to the regex parser.
    """
    ext = Path(filepath).suffix.lower()
    parser_class = _PARSERS.get(ext)
    if parser_class:
        instance = parser_class()
        if instance.can_parse():
            return instance
        # Fall back to regex parser
        fallback_class = _FALLBACK_PARSERS.get(ext)
        if fallback_class:
            return fallback_class()
    return None
