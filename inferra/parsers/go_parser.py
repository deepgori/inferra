"""
go_parser.py — Go Source File Parser

Regex-based parser for Go files. Extracts:
- Functions (func name(args) return)             — with Go 1.18+ generics
- Methods (func (receiver) name(args) return)    — with type params
- Structs (type Name struct { ... })
- Interfaces (type Name interface { ... })
- HTTP routes (http.HandleFunc, gin/echo/chi patterns)
- fmt.Print/log.Print patterns
"""

import re
from typing import List, Optional

from inferra.indexer import CodeUnit
from .base import LanguageParser, register_parser


@register_parser
class GoParser(LanguageParser):
    """Parser for Go source files."""

    LANGUAGE = "go"
    EXTENSIONS = [".go"]

    # NOTE: (?:\[[^\]]*\])? handles Go 1.18+ type params: func Map[T any](...)
    _FUNC_PATTERN = re.compile(
        r"^func\s+(\w+)\s*(?:\[[^\]]*\])?\s*\(([^)]*)\)\s*(.*?)\s*{",
        re.MULTILINE,
    )

    _METHOD_PATTERN = re.compile(
        r"^func\s+\((\w+)\s+\*?(\w+)\)\s+(\w+)\s*(?:\[[^\]]*\])?\s*\(([^)]*)\)\s*(.*?)\s*{",
        re.MULTILINE,
    )

    _STRUCT_PATTERN = re.compile(
        r"^type\s+(\w+)\s*(?:\[[^\]]*\])?\s+struct\s*{",
        re.MULTILINE,
    )

    _INTERFACE_PATTERN = re.compile(
        r"^type\s+(\w+)\s*(?:\[[^\]]*\])?\s+interface\s*{",
        re.MULTILINE,
    )

    _ROUTE_PATTERNS = [
        # http.HandleFunc("/path", handler)
        re.compile(
            r'(?:http\.HandleFunc|mux\.HandleFunc|r\.HandleFunc)\s*\(\s*"([^"]+)"',
        ),
        # Gin: r.GET("/path", handler)
        re.compile(
            r'(?:r|router|group|api|v1)\.(GET|POST|PUT|DELETE|PATCH|HEAD)\s*\(\s*"([^"]+)"',
            re.IGNORECASE,
        ),
        # Echo: e.GET("/path", handler)
        re.compile(
            r'(?:e|echo|group|api)\.(GET|POST|PUT|DELETE|PATCH)\s*\(\s*"([^"]+)"',
            re.IGNORECASE,
        ),
        # Chi: r.Get("/path", handler)
        re.compile(
            r'(?:r|router|mux)\.(Get|Post|Put|Delete|Patch)\s*\(\s*"([^"]+)"',
        ),
    ]

    _IMPORT_PATTERN = re.compile(r'"([^"]+)"')

    _CALL_PATTERN = re.compile(r"\b(\w+)\s*\(")

    def parse(
        self,
        source: str,
        filepath: str,
        module_name: str,
    ) -> List[CodeUnit]:
        units = []
        lines = source.split("\n")
        imports = self._extract_go_imports(source)
        route_map = self._extract_routes(source)

        # Parse structs
        for match in self._STRUCT_PATTERN.finditer(source):
            name = match.group(1)
            line_num = source[: match.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_num - 1)
            body = "\n".join(lines[line_num - 1 : end_line])

            units.append(
                CodeUnit(
                    name=name,
                    qualified_name=f"{module_name}.{name}",
                    unit_type="class",  # Go struct ≈ class
                    source_file=filepath,
                    start_line=line_num,
                    end_line=end_line,
                    signature=f"type {name} struct",
                    docstring=self._extract_go_doc(lines, line_num - 1),
                    body_text=body,
                    log_patterns=self._extract_log_patterns(body),
                    imports=imports,
                    calls=self._extract_calls(body),
                    tokens=self._tokenize(f"{name} struct {body}"),
                )
            )

        # Parse interfaces
        for match in self._INTERFACE_PATTERN.finditer(source):
            name = match.group(1)
            line_num = source[: match.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_num - 1)
            body = "\n".join(lines[line_num - 1 : end_line])

            units.append(
                CodeUnit(
                    name=name,
                    qualified_name=f"{module_name}.{name}",
                    unit_type="class",
                    source_file=filepath,
                    start_line=line_num,
                    end_line=end_line,
                    signature=f"type {name} interface",
                    docstring=self._extract_go_doc(lines, line_num - 1),
                    body_text=body,
                    log_patterns=self._extract_log_patterns(body),
                    imports=imports,
                    calls=self._extract_calls(body),
                    tokens=self._tokenize(f"{name} interface {body}"),
                )
            )

        # Parse methods (func (r *Receiver) Name(args) return)
        for match in self._METHOD_PATTERN.finditer(source):
            receiver_name = match.group(1)
            receiver_type = match.group(2)
            func_name = match.group(3)
            args = match.group(4)
            returns = match.group(5).strip()
            line_num = source[: match.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_num - 1)
            body = "\n".join(lines[line_num - 1 : end_line])
            sig = f"func ({receiver_name} *{receiver_type}) {func_name}({args})"
            if returns:
                sig += f" {returns}"

            units.append(
                CodeUnit(
                    name=func_name,
                    qualified_name=f"{module_name}.{receiver_type}.{func_name}",
                    unit_type="method",
                    source_file=filepath,
                    start_line=line_num,
                    end_line=end_line,
                    signature=sig,
                    docstring=self._extract_go_doc(lines, line_num - 1),
                    body_text=body,
                    log_patterns=self._extract_log_patterns(body),
                    imports=imports,
                    calls=self._extract_calls(body),
                    tokens=self._tokenize(f"{func_name} {receiver_type} {sig} {body}"),
                    route_path=route_map.get(func_name),
                )
            )

        # Parse standalone functions (skip init() — noise for RAG)
        for match in self._FUNC_PATTERN.finditer(source):
            func_name = match.group(1)
            if func_name == "init":
                continue
            args = match.group(2)
            returns = match.group(3).strip()
            line_num = source[: match.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_num - 1)
            body = "\n".join(lines[line_num - 1 : end_line])
            sig = f"func {func_name}({args})"
            if returns:
                sig += f" {returns}"

            units.append(
                CodeUnit(
                    name=func_name,
                    qualified_name=f"{module_name}.{func_name}",
                    unit_type="function",
                    source_file=filepath,
                    start_line=line_num,
                    end_line=end_line,
                    signature=sig,
                    docstring=self._extract_go_doc(lines, line_num - 1),
                    body_text=body,
                    log_patterns=self._extract_log_patterns(body),
                    imports=imports,
                    calls=self._extract_calls(body),
                    tokens=self._tokenize(f"{func_name} {sig} {body}"),
                    route_path=route_map.get(func_name),
                )
            )

        return units

    def _extract_go_imports(self, source: str) -> List[str]:
        imports = []
        import_block = re.search(r"import\s*\((.*?)\)", source, re.DOTALL)
        if import_block:
            for m in self._IMPORT_PATTERN.finditer(import_block.group(1)):
                imports.append(m.group(1))
        # Single imports
        for m in re.finditer(r'import\s+"([^"]+)"', source):
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
                    after = source[m.end() : m.end() + 100]
                    handler = re.search(r"\b(\w+)\b", after)
                    if handler:
                        routes[handler.group(1)] = f"{method} {path}"
                else:
                    path = m.group(1)
                    after = source[m.end() : m.end() + 100]
                    handler = re.search(r"\b(\w+)\b", after)
                    if handler:
                        routes[handler.group(1)] = f"ANY {path}"
        return routes

    def _extract_calls(self, body: str) -> List[str]:
        calls = set()
        for m in self._CALL_PATTERN.finditer(body):
            name = m.group(1)
            if name not in ("if", "for", "range", "switch", "case", "return",
                           "func", "make", "len", "cap", "append", "copy",
                           "delete", "new", "panic", "recover", "close",
                           "print", "println"):
                calls.add(name)
        return list(calls)

    def _extract_go_doc(self, lines: List[str], func_line: int) -> Optional[str]:
        """Extract Go documentation comment above a function."""
        doc_lines = []
        i = func_line - 1
        while i >= 0 and lines[i].strip().startswith("//"):
            doc_lines.insert(0, lines[i].strip().lstrip("/ "))
            i -= 1
        return "\n".join(doc_lines) if doc_lines else None

    def _find_block_end(self, lines: List[str], start: int) -> int:
        """Find the end of a braced block, ignoring braces in strings/comments."""
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

                if in_block_comment:
                    if ch == "*" and next_ch == "/":
                        in_block_comment = False
                        j += 2
                        continue
                    j += 1
                    continue

                if in_line_comment:
                    break

                if in_string:
                    if ch == "\\" and string_char != "`" and j + 1 < len(line):
                        j += 2
                        continue
                    if ch == string_char:
                        in_string = False
                    j += 1
                    continue

                if ch == "/" and next_ch == "/":
                    break
                if ch == "/" and next_ch == "*":
                    in_block_comment = True
                    j += 2
                    continue

                if ch in ('"', "`"):
                    in_string = True
                    string_char = ch
                    j += 1
                    continue

                if ch == "{":
                    depth += 1
                    found_open = True
                elif ch == "}":
                    depth -= 1
                    if found_open and depth <= 0:
                        return i + 1

                j += 1

        return min(start + 20, len(lines))
