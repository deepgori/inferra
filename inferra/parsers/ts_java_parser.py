"""
ts_java_parser.py — Tree-sitter Java Parser

Production-grade parser using tree-sitter AST.
Handles all Java constructs: records, sealed classes, generics,
annotations (including Spring @GetMapping), lambdas, enums.
"""

import logging
from typing import List, Optional

from inferra.indexer import CodeUnit
from .base import LanguageParser, register_parser

log = logging.getLogger("inferra.parser.ts_java")

try:
    import tree_sitter_java as tsjava
    from tree_sitter import Language, Parser
    JAVA_LANGUAGE = Language(tsjava.language())
    _HAS_TREESITTER = True
except ImportError:
    _HAS_TREESITTER = False


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_javadoc(node, source: bytes) -> str:
    """Find Javadoc comment preceding a node."""
    sibling = node.prev_sibling
    while sibling and sibling.type in ("annotation", "marker_annotation", "modifiers"):
        sibling = sibling.prev_sibling
    if sibling and sibling.type in ("block_comment", "comment"):
        text = _node_text(sibling, source)
        if text.startswith("/**"):
            return text
    return ""


def _get_annotations(node, source: bytes) -> List[str]:
    """Extract annotation strings from a node's modifiers."""
    annotations = []
    for child in node.children:
        if child.type in ("modifiers",):
            for mod in child.children:
                if mod.type in ("annotation", "marker_annotation"):
                    annotations.append(_node_text(mod, source))
    return annotations


@register_parser
class TreeSitterJavaParser(LanguageParser):
    """Tree-sitter based parser for Java files."""

    LANGUAGE = "java"
    EXTENSIONS = [".java"]

    def can_parse(self) -> bool:
        return _HAS_TREESITTER

    def parse(self, source: str, filepath: str, module_name: str) -> List[CodeUnit]:
        if not _HAS_TREESITTER:
            return []

        source_bytes = source.encode("utf-8")
        parser = Parser(JAVA_LANGUAGE)
        tree = parser.parse(source_bytes)
        units = []

        self._walk(tree.root_node, source_bytes, filepath, source, units)
        return units

    def _walk(self, node, source, filepath, content, units, class_name=""):
        if node.type == "class_declaration":
            unit = self._extract_class(node, source, filepath, content, "class")
            if unit:
                units.append(unit)
                # Walk class body for methods
                for child in node.children:
                    if child.type == "class_body":
                        for member in child.children:
                            self._walk(member, source, filepath, content, units,
                                       class_name=unit.name)
            return

        elif node.type == "interface_declaration":
            unit = self._extract_class(node, source, filepath, content, "interface")
            if unit:
                units.append(unit)
                for child in node.children:
                    if child.type == "interface_body":
                        for member in child.children:
                            self._walk(member, source, filepath, content, units,
                                       class_name=unit.name)
            return

        elif node.type == "enum_declaration":
            unit = self._extract_class(node, source, filepath, content, "enum")
            if unit:
                units.append(unit)
                for child in node.children:
                    if child.type == "enum_body":
                        for member in child.children:
                            self._walk(member, source, filepath, content, units,
                                       class_name=unit.name)
            return

        elif node.type == "record_declaration":
            unit = self._extract_class(node, source, filepath, content, "record")
            if unit:
                units.append(unit)
                for child in node.children:
                    if child.type == "class_body":
                        for member in child.children:
                            self._walk(member, source, filepath, content, units,
                                       class_name=unit.name)
            return

        elif node.type == "method_declaration":
            unit = self._extract_method(node, source, filepath, content, class_name)
            if unit:
                units.append(unit)

        elif node.type == "constructor_declaration":
            unit = self._extract_method(node, source, filepath, content, class_name,
                                        is_constructor=True)
            if unit:
                units.append(unit)

        # Recurse into children for top-level and nested types
        for child in node.children:
            self._walk(child, source, filepath, content, units, class_name)

    def _extract_class(self, node, source, filepath, content, kind="class"):
        """Extract class/interface/enum/record declarations."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)

        # Get superclass
        superclass = ""
        for child in node.children:
            if child.type == "superclass":
                for sc in child.children:
                    if sc.type == "type_identifier":
                        superclass = _node_text(sc, source)
                        break

        # Get type parameters (generics)
        type_params = ""
        tp_node = node.child_by_field_name("type_parameters")
        if tp_node:
            type_params = _node_text(tp_node, source)

        body_text = _node_text(node, source)
        javadoc = _find_javadoc(node, source)
        annotations = _get_annotations(node, source)

        sig = f"{kind} {name}{type_params}"
        if superclass:
            sig += f" extends {superclass}"

        return CodeUnit(
            name=name,
            qualified_name=name,
            unit_type="class",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=sig,
            docstring=javadoc,
            
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_method(self, node, source, filepath, content, class_name="",
                        is_constructor=False):
        """Extract method/constructor declarations."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)

        # Parameters
        params = ""
        params_node = node.child_by_field_name("parameters")
        if params_node:
            params = _node_text(params_node, source).strip("()")

        # Return type
        return_type = ""
        type_node = node.child_by_field_name("type")
        if type_node:
            return_type = _node_text(type_node, source)

        # Type parameters on method
        type_params = ""
        tp_node = node.child_by_field_name("type_parameters")
        if tp_node:
            type_params = _node_text(tp_node, source) + " "

        body_text = _node_text(node, source)
        javadoc = _find_javadoc(node, source)
        annotations = _get_annotations(node, source)

        # Detect Spring/JAX-RS routes
        route_path = self._extract_route_from_annotations(annotations)

        # Build signature
        if is_constructor:
            sig = f"{name}({params})"
        else:
            sig = f"{type_params}{return_type} {name}({params})"

        qualified = f"{class_name}.{name}" if class_name else name

        return CodeUnit(
            name=name,
            qualified_name=qualified,
            unit_type="method",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=sig,
            docstring=javadoc,
            
            route_path=route_path,
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_route_from_annotations(self, annotations: List[str]) -> str:
        """Extract HTTP route from Spring/JAX-RS annotations."""
        import re
        for ann in annotations:
            # Spring: @GetMapping("/path"), @PostMapping({"/path1", "/path2"})
            match = re.search(
                r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\'{]?"?([^"}\)]+)',
                ann
            )
            if match:
                method = match.group(1).upper()
                if method == "REQUEST":
                    method = "GET"
                path = match.group(2).strip('"\'')
                return f"{method} {path}"

            # JAX-RS: @GET @Path("/path")
            if re.search(r'@(GET|POST|PUT|DELETE|PATCH)', ann):
                method = re.search(r'@(GET|POST|PUT|DELETE|PATCH)', ann).group(1)
                path_match = re.search(r'@Path\s*\(\s*"([^"]+)"', " ".join(annotations))
                path = path_match.group(1) if path_match else "/"
                return f"{method} {path}"

        return ""
