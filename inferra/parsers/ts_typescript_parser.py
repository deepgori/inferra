"""
ts_typescript_parser.py — Tree-sitter TypeScript Parser

Handles TypeScript-specific constructs that the JavaScript parser misses:
- Enums (const enum, string enum)
- Interfaces
- Type aliases
- Decorators (@Controller, @Injectable, @Get)
- Type guards (x is SomeType)
- Namespaces / modules
- Abstract classes

Falls back to the JavaScript tree-sitter parser (which handles .ts/.tsx)
if tree-sitter-typescript is not installed.
"""

import logging
from typing import List, Optional

from inferra.indexer import CodeUnit
from .base import LanguageParser, register_parser

log = logging.getLogger("inferra.parser.ts_ts")

try:
    import tree_sitter_typescript as tsts
    from tree_sitter import Language, Parser
    TS_LANGUAGE = Language(tsts.language_typescript())
    TSX_LANGUAGE = Language(tsts.language_tsx())
    _HAS_TS_TREESITTER = True
except (ImportError, AttributeError):
    _HAS_TS_TREESITTER = False


def _node_text(node, source_bytes: bytes) -> str:
    """Extract text from a tree-sitter node."""
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_jsdoc(node, source_bytes: bytes) -> str:
    """Find JSDoc/TSDoc comment immediately preceding a node."""
    if node.prev_sibling and node.prev_sibling.type == "comment":
        text = _node_text(node.prev_sibling, source_bytes)
        if text.startswith("/**"):
            return text
    return ""


def _extract_decorators(node, source_bytes: bytes) -> List[str]:
    """Extract decorator strings from a node's preceding siblings."""
    decorators = []
    sibling = node.prev_sibling
    while sibling:
        if sibling.type == "decorator":
            decorators.append(_node_text(sibling, source_bytes))
        else:
            break
        sibling = sibling.prev_sibling
    return decorators


def _extract_route_from_decorators(decorators: List[str]) -> str:
    """Extract route path from decorators like @Get('/users'), @Post('/api/items')."""
    import re
    for dec in decorators:
        match = re.match(
            r"@(?:Get|Post|Put|Delete|Patch|Head|Options|All|RequestMapping)\s*\(\s*['\"](.+?)['\"]",
            dec,
        )
        if match:
            method = dec.split("(")[0].strip("@").upper()
            if method == "REQUESTMAPPING":
                method = "GET"
            return f"{method} {match.group(1)}"
    return ""


@register_parser
class TreeSitterTypeScriptParser(LanguageParser):
    """Tree-sitter parser for TypeScript files (.ts, .tsx).

    Handles TypeScript-specific constructs:
    - enum declarations (including const enum)
    - interface declarations
    - type alias declarations
    - decorators (@Controller, @Get, etc.)
    - abstract classes
    - namespace / module declarations
    - type guards (function return type `x is SomeType`)
    """

    LANGUAGE = "typescript"
    EXTENSIONS = [".ts", ".tsx"]

    def can_parse(self) -> bool:
        return _HAS_TS_TREESITTER

    def parse(self, source: str, filepath: str, module_name: str) -> List[CodeUnit]:
        if not _HAS_TS_TREESITTER:
            return []

        source_bytes = source.encode("utf-8")
        lang = TSX_LANGUAGE if filepath.endswith(".tsx") else TS_LANGUAGE
        parser = Parser(lang)
        tree = parser.parse(source_bytes)
        units = []

        self._walk(tree.root_node, source_bytes, filepath, units, source)
        return units

    def _walk(self, node, source_bytes: bytes, filepath: str, units: List[CodeUnit],
              content: str, class_name: str = ""):
        """Recursively walk the AST and extract TypeScript code units."""

        # ── Functions ──
        if node.type in ("function_declaration", "generator_function_declaration"):
            unit = self._extract_function(node, source_bytes, filepath, content, class_name)
            if unit:
                units.append(unit)

        # ── Export statements ──
        elif node.type == "export_statement":
            for child in node.children:
                self._walk(child, source_bytes, filepath, units, content, class_name)
            return

        # ── Variable declarations (arrow fns) ──
        elif node.type in ("lexical_declaration", "variable_declaration"):
            for declarator in node.children:
                if declarator.type == "variable_declarator":
                    unit = self._extract_variable_function(
                        declarator, node, source_bytes, filepath, content, class_name
                    )
                    if unit:
                        units.append(unit)

        # ── Class declarations (including abstract) ──
        elif node.type in ("class_declaration", "abstract_class_declaration"):
            unit = self._extract_class(node, source_bytes, filepath, content)
            if unit:
                units.append(unit)
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

        # ── Methods ──
        elif node.type in ("method_definition", "public_field_definition"):
            unit = self._extract_method(node, source_bytes, filepath, content, class_name)
            if unit:
                units.append(unit)

        # ── TypeScript-specific: Enums ──
        elif node.type == "enum_declaration":
            unit = self._extract_enum(node, source_bytes, filepath, content)
            if unit:
                units.append(unit)

        # ── TypeScript-specific: Interfaces ──
        elif node.type == "interface_declaration":
            unit = self._extract_interface(node, source_bytes, filepath, content)
            if unit:
                units.append(unit)

        # ── TypeScript-specific: Type Aliases ──
        elif node.type == "type_alias_declaration":
            unit = self._extract_type_alias(node, source_bytes, filepath, content)
            if unit:
                units.append(unit)

        # ── TypeScript-specific: Namespaces/Modules ──
        elif node.type in ("module", "internal_module"):
            unit = self._extract_namespace(node, source_bytes, filepath, content, units)
            if unit:
                units.append(unit)
            return  # Namespace handles its own recursion

        # ── Expression statements (routes) ──
        elif node.type == "expression_statement":
            route = self._extract_route(node, source_bytes, filepath, content)
            if route:
                units.append(route)

        # Recurse into children
        for child in node.children:
            self._walk(child, source_bytes, filepath, units, content, class_name)

    # ── Function Extraction ──────────────────────────────────────────────────

    def _extract_function(self, node, source, filepath, content, class_name=""):
        """Extract a function declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)
        params = self._get_params(node, source)
        return_type = self._get_return_type(node, source)
        body_text = _node_text(node, source)
        jsdoc = _find_jsdoc(node, source)
        is_async = any(c.type == "async" for c in node.children)

        # Check for type guard return
        is_type_guard = " is " in return_type if return_type else False

        sig = f"{'async ' if is_async else ''}function {name}({params})"
        if return_type:
            sig += f": {return_type}"

        return CodeUnit(
            name=name,
            qualified_name=f"{class_name}.{name}" if class_name else name,
            unit_type="type_guard" if is_type_guard else ("async_function" if is_async else "function"),
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=sig,
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

        func_types = ("arrow_function", "function_expression",
                      "generator_function", "function")
        actual_value = value_node

        # Handle wrapper nodes
        if actual_value.type not in func_types:
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

        return CodeUnit(
            name=name,
            qualified_name=f"{class_name}.{name}" if class_name else name,
            unit_type="async_function" if is_async else "function",
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

    # ── Class Extraction ─────────────────────────────────────────────────────

    def _extract_class(self, node, source, filepath, content):
        """Extract a class declaration (including abstract classes)."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)

        is_abstract = node.type == "abstract_class_declaration" or \
            any(c.type == "abstract" for c in node.children)

        superclass = ""
        heritage = node.child_by_field_name("superclass")
        if not heritage:
            for child in node.children:
                if child.type == "class_heritage":
                    for hc in child.children:
                        if hc.type == "identifier":
                            superclass = _node_text(hc, source)
                            break
        else:
            superclass = _node_text(heritage, source)

        # Check for implements
        implements = []
        for child in node.children:
            if child.type == "implements_clause":
                for hc in child.children:
                    if hc.type in ("type_identifier", "generic_type"):
                        implements.append(_node_text(hc, source))

        decorators = _extract_decorators(node, source)
        body_text = _node_text(node, source)
        jsdoc = _find_jsdoc(node, source)

        sig = f"{'abstract ' if is_abstract else ''}class {name}"
        if superclass:
            sig += f" extends {superclass}"
        if implements:
            sig += f" implements {', '.join(implements)}"

        return CodeUnit(
            name=name,
            qualified_name=name,
            unit_type="abstract_class" if is_abstract else "class",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=sig,
            docstring=jsdoc,
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_method(self, node, source, filepath, content, class_name=""):
        """Extract a class method (including decorated methods)."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)
        params = self._get_params(node, source)
        return_type = self._get_return_type(node, source)
        body_text = _node_text(node, source)
        is_async = any(c.type == "async" for c in node.children)

        decorators = _extract_decorators(node, source)
        route_path = _extract_route_from_decorators(decorators)

        sig = f"{'async ' if is_async else ''}{name}({params})"
        if return_type:
            sig += f": {return_type}"
        if decorators:
            sig = "\n".join(decorators) + "\n" + sig

        unit = CodeUnit(
            name=name,
            qualified_name=f"{class_name}.{name}" if class_name else name,
            unit_type="method",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=sig,
            docstring="",
            imports=[],
            calls=[],
            log_patterns=[],
        )
        if route_path:
            unit.route_path = route_path
        return unit

    # ── TypeScript-Specific Constructs ───────────────────────────────────────

    def _extract_enum(self, node, source, filepath, content):
        """Extract an enum declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)
        body_text = _node_text(node, source)
        is_const = any(c.type == "const" for c in node.children)

        # Extract member names
        members = []
        for child in node.children:
            if child.type == "enum_body":
                for member in child.children:
                    if member.type == "enum_member":
                        member_name = member.child_by_field_name("name")
                        if member_name:
                            members.append(_node_text(member_name, source))

        return CodeUnit(
            name=name,
            qualified_name=name,
            unit_type="enum",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=f"{'const ' if is_const else ''}enum {name} {{ {', '.join(members[:8])}{'...' if len(members) > 8 else ''} }}",
            docstring=_find_jsdoc(node, source),
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_interface(self, node, source, filepath, content):
        """Extract an interface declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)
        body_text = _node_text(node, source)

        # Check for extends
        extends = []
        for child in node.children:
            if child.type == "extends_type_clause":
                for hc in child.children:
                    if hc.type in ("type_identifier", "generic_type"):
                        extends.append(_node_text(hc, source))

        # Extract type parameters
        type_params = ""
        for child in node.children:
            if child.type == "type_parameters":
                type_params = _node_text(child, source)
                break

        sig = f"interface {name}{type_params}"
        if extends:
            sig += f" extends {', '.join(extends)}"

        return CodeUnit(
            name=name,
            qualified_name=name,
            unit_type="interface",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=sig,
            docstring=_find_jsdoc(node, source),
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_type_alias(self, node, source, filepath, content):
        """Extract a type alias declaration (type Foo = ...)."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)
        body_text = _node_text(node, source)

        # Get the type value
        value_node = node.child_by_field_name("value")
        type_value = _node_text(value_node, source) if value_node else "..."

        # Truncate long type definitions
        if len(type_value) > 100:
            type_value = type_value[:97] + "..."

        return CodeUnit(
            name=name,
            qualified_name=name,
            unit_type="type_alias",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=f"type {name} = {type_value}",
            docstring=_find_jsdoc(node, source),
            imports=[],
            calls=[],
            log_patterns=[],
        )

    def _extract_namespace(self, node, source, filepath, content, units):
        """Extract a namespace/module declaration and recurse into its body."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node, source)
        body_text = _node_text(node, source)

        unit = CodeUnit(
            name=name,
            qualified_name=name,
            unit_type="namespace",
            source_file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            body_text=body_text,
            signature=f"namespace {name}",
            docstring=_find_jsdoc(node, source),
            imports=[],
            calls=[],
            log_patterns=[],
        )

        # Recurse into namespace body
        for child in node.children:
            if child.type == "statement_block":
                for stmt in child.children:
                    self._walk(stmt, source, filepath, units, content.encode() if isinstance(content, str) else content,
                              class_name=name)

        return unit

    # ── Route Extraction ─────────────────────────────────────────────────────

    def _extract_route(self, node, source, filepath, content):
        """Extract Express/NestJS route registrations from expression statements."""
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_params(self, node, source):
        """Extract parameter text from a function node."""
        params_node = node.child_by_field_name("parameters")
        if not params_node:
            for child in node.children:
                if child.type == "formal_parameters":
                    params_node = child
                    break
        if params_node:
            text = _node_text(params_node, source)
            return text.strip("()")
        return ""

    def _get_return_type(self, node, source):
        """Extract return type annotation from a function node."""
        for child in node.children:
            if child.type == "type_annotation":
                return _node_text(child, source).lstrip(":").strip()
        return_type = node.child_by_field_name("return_type")
        if return_type:
            return _node_text(return_type, source).lstrip(":").strip()
        return ""
