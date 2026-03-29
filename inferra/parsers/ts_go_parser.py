"""
ts_go_parser.py — Tree-sitter Go Parser

Production-grade parser using tree-sitter AST.
Handles all Go constructs: generics (1.18+), methods with receivers,
multi-line signatures, embedded structs, Gin/Echo/Chi routes.
"""

import logging
from typing import List, Optional

from inferra.indexer import CodeUnit
from .base import LanguageParser, register_parser

log = logging.getLogger("inferra.parser.ts_go")

try:
    import tree_sitter_go as tsgo
    from tree_sitter import Language, Parser
    GO_LANGUAGE = Language(tsgo.language())
    _HAS_TREESITTER = True
except ImportError:
    _HAS_TREESITTER = False


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_godoc(node, source: bytes) -> str:
    """Find Go doc comment immediately preceding a node."""
    if node.prev_sibling and node.prev_sibling.type == "comment":
        return _node_text(node.prev_sibling, source)
    return ""


@register_parser
class TreeSitterGoParser(LanguageParser):
    """Tree-sitter based parser for Go files."""

    LANGUAGE = "go"
    EXTENSIONS = [".go"]

    def can_parse(self) -> bool:
        return _HAS_TREESITTER

    def parse(self, source: str, filepath: str, module_name: str) -> List[CodeUnit]:
        if not _HAS_TREESITTER:
            return []

        source_bytes = source.encode("utf-8")
        parser = Parser(GO_LANGUAGE)
        tree = parser.parse(source_bytes)
        units = []

        self._walk(tree.root_node, source_bytes, filepath, source, units)
        return units

    def _walk(self, node, source, filepath, content, units):
        if node.type == "function_declaration":
            unit = self._extract_function(node, source, filepath, content)
            if unit:
                units.append(unit)

        elif node.type == "method_declaration":
            unit = self._extract_method(node, source, filepath, content)
            if unit:
                units.append(unit)

        elif node.type == "type_declaration":
            for child in node.children:
                if child.type == "type_spec":
                    unit = self._extract_type(child, node, source, filepath, content)
                    if unit:
                        units.append(unit)

        # Recurse
        for child in node.children:
            self._walk(child, source, filepath, content, units)

    def _extract_function(self, node, source, filepath, content):
        """Extract a Go function declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)
        params = self._get_params(node, source)
        body_text = _node_text(node, source)
        godoc = _find_godoc(node, source)

        # Check for type parameters (generics)
        type_params = ""
        tp_node = node.child_by_field_name("type_parameters")
        if tp_node:
            type_params = _node_text(tp_node, source)

        # Detect route registrations in body
        route_path = self._detect_route(body_text, name)

        return CodeUnit(
            name=name,
            qualified_name=name,
            unit_type="function",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=f"func {name}{type_params}({params})",
            docstring=godoc,
            route_path=route_path,
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_method(self, node, source, filepath, content):
        """Extract a Go method (function with receiver)."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)

        # Get receiver type
        receiver = ""
        recv_node = node.child_by_field_name("receiver")
        if recv_node:
            # Extract the type name from the parameter list
            recv_text = _node_text(recv_node, source).strip("()")
            parts = recv_text.split()
            if parts:
                receiver = parts[-1].strip("*")

        params = self._get_params(node, source)
        body_text = _node_text(node, source)
        godoc = _find_godoc(node, source)

        qualified = f"{receiver}.{name}" if receiver else name
        route_path = self._detect_route(body_text, name)

        return CodeUnit(
            name=name,
            qualified_name=qualified,
            unit_type="method",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=f"func ({receiver}) {name}({params})",
            docstring=godoc,
            route_path=route_path,
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_type(self, type_spec, parent_node, source, filepath, content):
        """Extract struct/interface type declarations."""
        name_node = type_spec.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)

        # Determine if it's a struct or interface
        type_node = type_spec.child_by_field_name("type")
        unit_type = "class"  # Default
        sig_prefix = "type"
        if type_node:
            if type_node.type == "struct_type":
                sig_prefix = "struct"
            elif type_node.type == "interface_type":
                sig_prefix = "interface"

        # Check for type parameters (generics)
        type_params = ""
        tp_node = type_spec.child_by_field_name("type_parameters")
        if tp_node:
            type_params = _node_text(tp_node, source)

        body_text = _node_text(parent_node, source)
        godoc = _find_godoc(parent_node, source)

        return CodeUnit(
            name=name,
            qualified_name=name,
            unit_type=unit_type,
            source_file=filepath,
            start_line=parent_node.start_point[0] + 1,
            end_line=parent_node.end_point[0] + 1,
            body_text=body_text,
            signature=f"type {name}{type_params} {sig_prefix}",
            docstring=godoc,
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _get_params(self, node, source):
        """Extract parameter list text."""
        params_node = node.child_by_field_name("parameters")
        if params_node:
            text = _node_text(params_node, source)
            return text.strip("()")
        return ""

    def _detect_route(self, body_text, func_name):
        """Detect Gin/Echo/Chi route patterns in function body."""
        import re
        # Gin: r.GET("/path", handler), r.POST("/path", handler)
        # Echo: e.GET("/path", handler)
        # Chi: r.Get("/path", handler)
        match = re.search(
            r'\.(?:GET|POST|PUT|DELETE|PATCH|Get|Post|Put|Delete|Patch)\s*\(\s*"([^"]+)"',
            body_text
        )
        if match:
            route = match.group(1)
            method_match = re.search(
                r'\.(GET|POST|PUT|DELETE|PATCH|Get|Post|Put|Delete|Patch)\s*\(',
                body_text
            )
            if method_match:
                method = method_match.group(1).upper()
                return f"{method} {route}"
        return ""
