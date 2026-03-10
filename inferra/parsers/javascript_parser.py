"""
javascript_parser.py — JavaScript/TypeScript Parser

Regex-based parser for JS/TS files. Extracts:
- Functions (function declarations, arrow functions, methods)
- Generator functions (function*)
- Classes (with generic type params)
- TypeScript interfaces and type aliases
- Express/Next.js/Fastify routes
- Console.log patterns
- Import statements
"""

import re
from typing import List, Optional

from inferra.indexer import CodeUnit
from .base import LanguageParser, register_parser


@register_parser
class JavaScriptParser(LanguageParser):
    """Parser for JavaScript and TypeScript files."""

    LANGUAGE = "javascript"
    EXTENSIONS = [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"]

    # NOTE: (?:<[^>]*>)? allows optional TS generics like <T> or <T extends Base>
    _FUNC_PATTERNS = [
        # function name<T>(args) { ... }  or  function* name(args) { ... }
        re.compile(
            r"^(?:export\s+)?(?:async\s+)?function\s*\*?\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)",
            re.MULTILINE,
        ),
        # const name = <T,>(args): RetType => { ... }  (generic arrow)
        re.compile(
            r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?<[^>]*>\s*\(([^)]*)\)\s*(?::\s*[^=]*?)?\s*=>",
            re.MULTILINE,
        ),
        # const name = (args): ReturnType => { ... }
        re.compile(
            r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(([^)]*)\)\s*(?::\s*[^=]*?)?\s*=>",
            re.MULTILINE,
        ),
        # const name = async function<T>(args) { ... }
        re.compile(
            r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function\s*\*?\s*(?:<[^>]*>)?\s*\(([^)]*)\)",
            re.MULTILINE,
        ),
    ]

    _CLASS_PATTERN = re.compile(
        r"^(?:export\s+)?(?:default\s+)?class\s+(\w+)\s*(?:<[^>]*>)?\s*(?:extends\s+(\w+)(?:\s*<[^>]*>)?)?",
        re.MULTILINE,
    )

    # TypeScript interface → extracted as class-like unit
    _INTERFACE_PATTERN = re.compile(
        r"^(?:export\s+)?interface\s+(\w+)\s*(?:<[^>]*>)?\s*(?:extends\s+([\w,\s]+))?\s*\{",
        re.MULTILINE,
    )

    # TypeScript type alias → extracted as class-like unit
    _TYPE_PATTERN = re.compile(
        r"^(?:export\s+)?type\s+(\w+)\s*(?:<[^>]*>)?\s*=",
        re.MULTILINE,
    )

    # Class methods: allow optional generics and complex TS return types
    _METHOD_PATTERN = re.compile(
        r"^\s+(?:async\s+)?(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?::\s*[^{]*)?\s*\{",
        re.MULTILINE,
    )

    _ROUTE_PATTERNS = [
        # Express: app.get('/path', ...) or router.get('/path', ...)
        re.compile(
            r"(?:app|router|server)\.(get|post|put|delete|patch|head|options)\s*\(\s*['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        ),
        # Next.js: export async function GET/POST/PUT/DELETE(req)
        re.compile(
            r"export\s+(?:async\s+)?function\s+(GET|POST|PUT|DELETE|PATCH)\s*\(",
            re.IGNORECASE,
        ),
        # Fastify: fastify.get('/path', ...)
        re.compile(
            r"(?:fastify|server|instance)\.(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        ),
    ]

    _IMPORT_PATTERNS = [
        re.compile(r"import\s+(?:\{[^}]+\}|\w+)\s+from\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"const\s+\w+\s*=\s*require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    ]

    _CALL_PATTERN = re.compile(r"\b(\w+)\s*\(")

    def parse(
        self,
        source: str,
        filepath: str,
        module_name: str,
    ) -> List[CodeUnit]:
        units = []
        lines = source.split("\n")
        imports = self._extract_imports(source)

        # Extract route map for later matching
        route_map = self._extract_routes(source)

        # Parse classes
        for match in self._CLASS_PATTERN.finditer(source):
            class_name = match.group(1)
            extends = match.group(2) or ""
            line_num = source[: match.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_num - 1)
            body = "\n".join(lines[line_num - 1 : end_line])
            sig = f"class {class_name}" + (f" extends {extends}" if extends else "")

            units.append(
                CodeUnit(
                    name=class_name,
                    qualified_name=f"{module_name}.{class_name}",
                    unit_type="class",
                    source_file=filepath,
                    start_line=line_num,
                    end_line=end_line,
                    signature=sig,
                    docstring=self._extract_jsdoc(lines, line_num - 1),
                    body_text=body,
                    log_patterns=self._extract_log_patterns(body),
                    imports=imports,
                    calls=self._extract_calls(body),
                    tokens=self._tokenize(f"{class_name} {sig} {body}"),
                )
            )

            # Parse methods inside class
            for m_match in self._METHOD_PATTERN.finditer(body):
                method_name = m_match.group(1)
                method_args = m_match.group(2)
                m_line = line_num + body[: m_match.start()].count("\n")
                m_end = self._find_block_end(lines, m_line - 1)
                m_body = "\n".join(lines[m_line - 1 : m_end])
                m_sig = f"{method_name}({method_args})"

                if method_name in ("constructor", "if", "for", "while", "switch", "catch"):
                    continue

                units.append(
                    CodeUnit(
                        name=method_name,
                        qualified_name=f"{module_name}.{class_name}.{method_name}",
                        unit_type="method",
                        source_file=filepath,
                        start_line=m_line,
                        end_line=m_end,
                        signature=m_sig,
                        docstring=self._extract_jsdoc(lines, m_line - 1),
                        body_text=m_body,
                        log_patterns=self._extract_log_patterns(m_body),
                        imports=imports,
                        calls=self._extract_calls(m_body),
                        tokens=self._tokenize(f"{method_name} {m_sig} {m_body}"),
                    )
                )

        # Parse TypeScript interfaces
        for match in self._INTERFACE_PATTERN.finditer(source):
            iface_name = match.group(1)
            extends = match.group(2) or ""
            line_num = source[: match.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_num - 1)
            body = "\n".join(lines[line_num - 1 : end_line])
            sig = f"interface {iface_name}" + (f" extends {extends.strip()}" if extends else "")

            units.append(
                CodeUnit(
                    name=iface_name,
                    qualified_name=f"{module_name}.{iface_name}",
                    unit_type="class",
                    source_file=filepath,
                    start_line=line_num,
                    end_line=end_line,
                    signature=sig,
                    docstring=self._extract_jsdoc(lines, line_num - 1),
                    body_text=body,
                    log_patterns=[],
                    imports=imports,
                    calls=[],
                    tokens=self._tokenize(f"{iface_name} {sig} {body}"),
                )
            )

        # Parse TypeScript type aliases
        for match in self._TYPE_PATTERN.finditer(source):
            type_name = match.group(1)
            line_num = source[: match.start()].count("\n") + 1
            # Type aliases are usually single-line or a few lines
            end_line = min(line_num + 10, len(lines))
            # Find the end by looking for a line that doesn't end with | or &
            for i in range(line_num, end_line):
                stripped = lines[i - 1].rstrip()
                if stripped.endswith(";") or (not stripped.endswith("|") and not stripped.endswith("&") and i > line_num):
                    end_line = i
                    break
            body = "\n".join(lines[line_num - 1 : end_line])

            units.append(
                CodeUnit(
                    name=type_name,
                    qualified_name=f"{module_name}.{type_name}",
                    unit_type="class",
                    source_file=filepath,
                    start_line=line_num,
                    end_line=end_line,
                    signature=f"type {type_name}",
                    docstring=self._extract_jsdoc(lines, line_num - 1),
                    body_text=body,
                    log_patterns=[],
                    imports=imports,
                    calls=[],
                    tokens=self._tokenize(f"{type_name} type {body}"),
                )
            )

        # Parse standalone functions
        for pattern in self._FUNC_PATTERNS:
            for match in pattern.finditer(source):
                func_name = match.group(1)
                func_args = match.group(2) if match.lastindex >= 2 else ""
                line_num = source[: match.start()].count("\n") + 1
                end_line = self._find_block_end(lines, line_num - 1)
                body = "\n".join(lines[line_num - 1 : end_line])

                is_async = "async" in match.group(0)
                sig = f"{'async ' if is_async else ''}function {func_name}({func_args})"
                route = route_map.get(func_name)

                units.append(
                    CodeUnit(
                        name=func_name,
                        qualified_name=f"{module_name}.{func_name}",
                        unit_type="async_function" if is_async else "function",
                        source_file=filepath,
                        start_line=line_num,
                        end_line=end_line,
                        signature=sig,
                        docstring=self._extract_jsdoc(lines, line_num - 1),
                        body_text=body,
                        log_patterns=self._extract_log_patterns(body),
                        imports=imports,
                        calls=self._extract_calls(body),
                        tokens=self._tokenize(f"{func_name} {sig} {body}"),
                        route_path=route,
                    )
                )

        return units

    def _extract_imports(self, source: str) -> List[str]:
        imports = []
        for pattern in self._IMPORT_PATTERNS:
            for m in pattern.finditer(source):
                imports.append(m.group(1))
        return imports

    def _extract_routes(self, source: str) -> dict:
        """Build map: handler_function_name -> 'METHOD /path'."""
        routes = {}
        for pattern in self._ROUTE_PATTERNS:
            for m in pattern.finditer(source):
                if m.lastindex >= 2:
                    method = m.group(1).upper()
                    path = m.group(2)
                    # Try to find the handler name from the next function-like token
                    after = source[m.end() : m.end() + 200]
                    handler = re.search(r"\b(\w+)\b", after)
                    if handler and handler.group(1) not in ("req", "res", "ctx", "next", "async"):
                        routes[handler.group(1)] = f"{method} {path}"
                else:
                    # Next.js style: the function IS the handler
                    method = m.group(1).upper()
                    routes[method] = f"{method} /"
        return routes

    def _extract_calls(self, body: str) -> List[str]:
        calls = set()
        for m in self._CALL_PATTERN.finditer(body):
            name = m.group(1)
            if name not in ("if", "for", "while", "switch", "catch", "return",
                           "console", "require", "import", "export", "new"):
                calls.add(name)
        return list(calls)

    def _extract_jsdoc(self, lines: List[str], func_line: int) -> Optional[str]:
        """Extract JSDoc comment above a function."""
        if func_line <= 0:
            return None
        above = func_line - 1
        if above >= 0 and lines[above].strip().endswith("*/"):
            doc_lines = []
            while above >= 0:
                doc_lines.insert(0, lines[above].strip())
                if lines[above].strip().startswith("/**"):
                    break
                above -= 1
            return "\n".join(doc_lines)
        return None

    def _find_block_end(self, lines: List[str], start: int) -> int:
        """Find the end of a braced block, ignoring braces inside strings/comments."""
        depth = 0
        found_open = False
        in_string = False
        string_char = None
        in_line_comment = False
        in_block_comment = False

        for i in range(start, min(start + 500, len(lines))):
            line = lines[i]
            in_line_comment = False
            j = 0
            while j < len(line):
                ch = line[j]
                next_ch = line[j + 1] if j + 1 < len(line) else ""

                # Handle block comment end
                if in_block_comment:
                    if ch == "*" and next_ch == "/":
                        in_block_comment = False
                        j += 2
                        continue
                    j += 1
                    continue

                # Handle line comment
                if in_line_comment:
                    break  # rest of line is comment

                # Handle string state
                if in_string:
                    if ch == "\\" and j + 1 < len(line):
                        j += 2  # skip escaped character
                        continue
                    if ch == string_char:
                        in_string = False
                    j += 1
                    continue

                # Detect comment starts
                if ch == "/" and next_ch == "/":
                    in_line_comment = True
                    break
                if ch == "/" and next_ch == "*":
                    in_block_comment = True
                    j += 2
                    continue

                # Detect string starts
                if ch in ('"', "'", "`"):
                    in_string = True
                    string_char = ch
                    j += 1
                    continue

                # Count braces
                if ch == "{":
                    depth += 1
                    found_open = True
                elif ch == "}":
                    depth -= 1
                    if found_open and depth <= 0:
                        return i + 1

                j += 1

        return min(start + 20, len(lines))
