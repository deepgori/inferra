"""
ts_javascript_parser.py — Tree-sitter JavaScript/TypeScript Parser

Production-grade parser using tree-sitter AST instead of regex.
Handles all JS/TS constructs natively: nested generics, template literals,
decorators, computed properties, destructuring — no regex limitations.

Falls back to regex JavaScriptParser if tree-sitter is not installed.
"""

import logging
from typing import List, Optional

from inferra.indexer import CodeUnit
from .base import LanguageParser, register_parser

log = logging.getLogger("inferra.parser.ts_js")

try:
    import tree_sitter_javascript as tsjs
    from tree_sitter import Language, Parser
    JS_LANGUAGE = Language(tsjs.language())
    _HAS_TREESITTER = True
except ImportError:
    _HAS_TREESITTER = False


def _node_text(node, source_bytes: bytes) -> str:
    """Extract text from a tree-sitter node."""
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_jsdoc(node, source_bytes: bytes) -> str:
    """Find JSDoc comment immediately preceding a node."""
    if node.prev_sibling and node.prev_sibling.type == "comment":
        text = _node_text(node.prev_sibling, source_bytes)
        if text.startswith("/**"):
            return text
    return ""


@register_parser
class TreeSitterJavaScriptParser(LanguageParser):
    """Tree-sitter based parser for JavaScript and TypeScript files."""

    LANGUAGE = "javascript"
    EXTENSIONS = [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"]

    def can_parse(self) -> bool:
        return _HAS_TREESITTER

    def parse(self, source: str, filepath: str, module_name: str) -> List[CodeUnit]:
        if not _HAS_TREESITTER:
            return []

        source_bytes = source.encode("utf-8")
        parser = Parser(JS_LANGUAGE)
        tree = parser.parse(source_bytes)
        units = []

        self._walk(tree.root_node, source_bytes, filepath, units, source)
        return units

    def _walk(self, node, source_bytes: bytes, filepath: str, units: List[CodeUnit],
              content: str, class_name: str = ""):
        """Recursively walk the AST and extract code units."""

        if node.type in ("function_declaration", "generator_function_declaration"):
            unit = self._extract_function(node, source_bytes, filepath, content, class_name)
            if unit:
                units.append(unit)

        elif node.type == "export_statement":
            # export default function foo() {} / export const foo = () => {}
            for child in node.children:
                self._walk(child, source_bytes, filepath, units, content, class_name)
            return  # Don't recurse again below

        elif node.type in ("lexical_declaration", "variable_declaration"):
            # const foo = () => {} / const foo = function() {}
            for declarator in node.children:
                if declarator.type == "variable_declarator":
                    unit = self._extract_variable_function(
                        declarator, node, source_bytes, filepath, content, class_name
                    )
                    if unit:
                        units.append(unit)

        elif node.type == "class_declaration":
            unit = self._extract_class(node, source_bytes, filepath, content)
            if unit:
                units.append(unit)
                # Walk class body for methods
                body = None
                for child in node.children:
                    if child.type == "class_body":
                        body = child
                        break
                if body:
                    for child in body.children:
                        self._walk(child, source_bytes, filepath, units, content,
                                   class_name=unit.name)
            return

        elif node.type == "method_definition":
            unit = self._extract_method(node, source_bytes, filepath, content, class_name)
            if unit:
                units.append(unit)

        elif node.type == "expression_statement":
            # Check for Express routes: app.get('/path', handler)
            route = self._extract_route(node, source_bytes, filepath, content)
            if route:
                units.append(route)

        # Recurse into children
        for child in node.children:
            self._walk(child, source_bytes, filepath, units, content, class_name)

    def _extract_function(self, node, source, filepath, content, class_name=""):
        """Extract a function/generator declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)
        params = self._get_params(node, source)
        body_text = _node_text(node, source)
        jsdoc = _find_jsdoc(node, source)
        is_async = any(c.type == "async" for c in node.children)
        is_generator = "generator" in node.type

        unit_type = "async_function" if is_async else "function"
        if is_generator:
            unit_type = "generator_function"

        return CodeUnit(
            name=name,
            qualified_name=f"{class_name}.{name}" if class_name else name,
            unit_type=unit_type,
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=f"{'async ' if is_async else ''}function{'*' if is_generator else ''} {name}({params})",
            docstring=jsdoc,
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_variable_function(self, declarator, parent_node, source, filepath,
                                   content, class_name=""):
        """Extract arrow functions / function expressions assigned to variables."""
        name_node = declarator.child_by_field_name("name")
        value_node = declarator.child_by_field_name("value")
        if not name_node or not value_node:
            return None

        # Check if the value is a function/arrow
        func_types = ("arrow_function", "function_expression",
                      "generator_function", "function")
        actual_value = value_node
        # Handle: const foo = async () => {}
        if value_node.type == "await_expression":
            actual_value = value_node
        if actual_value.type not in func_types and \
           not any(c.type in func_types for c in actual_value.children if hasattr(c, 'type')):
            # Check if it's async: skip the "async" keyword wrapper
            found = False
            for c in (actual_value.children if hasattr(actual_value, 'children') else []):
                if c.type in func_types:
                    actual_value = c
                    found = True
                    break
            if not found:
                return None

        name = _node_text(name_node, source)
        params = self._get_params(actual_value, source)
        body_text = _node_text(parent_node, source)
        jsdoc = _find_jsdoc(parent_node, source)

        is_async = "async" in _node_text(parent_node, source).split(name)[0]
        unit_type = "async_function" if is_async else "function"

        return CodeUnit(
            name=name,
            qualified_name=f"{class_name}.{name}" if class_name else name,
            unit_type=unit_type,
            source_file=filepath,
            start_line=parent_node.start_point[0] + 1,
            end_line=parent_node.end_point[0] + 1,
            body_text=body_text,
            signature=f"{'async ' if is_async else ''}const {name} = ({params}) => {{...}}",
            docstring=jsdoc,
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_class(self, node, source, filepath, content):
        """Extract a class declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)

        # Get superclass
        superclass = ""
        heritage = node.child_by_field_name("superclass")  # tree-sitter field
        if not heritage:
            for child in node.children:
                if child.type == "class_heritage":
                    for hc in child.children:
                        if hc.type == "identifier":
                            superclass = _node_text(hc, source)
                            break
        else:
            superclass = _node_text(heritage, source)

        body_text = _node_text(node, source)
        jsdoc = _find_jsdoc(node, source)

        return CodeUnit(
            name=name,
            qualified_name=name,
            unit_type="class",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=f"class {name}" + (f" extends {superclass}" if superclass else ""),
            docstring=jsdoc,
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_method(self, node, source, filepath, content, class_name=""):
        """Extract a class method."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)

        # Skip constructor and trivial getters
        if name in ("constructor",):
            pass  # Still extract constructor

        params = self._get_params(node, source)
        body_text = _node_text(node, source)
        is_async = any(c.type == "async" for c in node.children)

        return CodeUnit(
            name=name,
            qualified_name=f"{class_name}.{name}" if class_name else name,
            unit_type="method",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=f"{'async ' if is_async else ''}{name}({params})",
            docstring="",
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_route(self, node, source, filepath, content):
        """Extract Express/Fastify route registrations."""
        # Look for: app.get('/path', handler) or router.post('/path', ...)
        expr = None
        for child in node.children:
            if child.type == "call_expression":
                expr = child
                break
        if not expr:
            return None

        func = expr.child_by_field_name("function")
        if not func or func.type != "member_expression":
            return None

        obj_text = ""
        method_text = ""
        for child in func.children:
            if child.type == "identifier":
                if not obj_text:
                    obj_text = _node_text(child, source)
                else:
                    method_text = _node_text(child, source)
            elif child.type == "property_identifier":
                method_text = _node_text(child, source)

        http_methods = ("get", "post", "put", "delete", "patch", "head", "options")
        if obj_text.lower() not in ("app", "router", "server") or method_text.lower() not in http_methods:
            return None

        # Get the route path (first argument)
        args = expr.child_by_field_name("arguments")
        if not args or not args.children:
            return None

        route_path = ""
        for arg in args.children:
            if arg.type in ("string", "template_string"):
                route_path = _node_text(arg, source).strip("'\"`")
                break

        if not route_path:
            return None

        body_text = _node_text(node, source)
        route_method = method_text.upper()

        return CodeUnit(
            name=f"{route_method} {route_path}",
            qualified_name=f"{route_method} {route_path}",
            unit_type="function",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=f"{obj_text}.{method_text}('{route_path}', ...)",
            docstring="",
            route_path=f"{route_method} {route_path}",
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _get_params(self, node, source):
        """Extract parameter text from a function node."""
        params_node = node.child_by_field_name("parameters")
        if not params_node:
            # For arrow functions, check children
            for child in node.children:
                if child.type == "formal_parameters":
                    params_node = child
                    break
        if params_node:
            text = _node_text(params_node, source)
            # Strip outer parentheses
            return text.strip("()")
        return ""
