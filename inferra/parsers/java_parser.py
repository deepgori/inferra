"""
java_parser.py — Java Source File Parser

Regex-based parser for Java files. Extracts:
- Classes, interfaces, enums, and records (Java 16+)
- Sealed classes (Java 17+)
- Methods (with generic return types like ResponseEntity<UserDTO>)
- Methods with annotations (@Override, etc.)
- Package-private methods (no access modifier)
- Spring Boot routes (@GetMapping, @PostMapping, etc.)
- JAX-RS routes (@GET @Path, etc.)
- JavaDoc comments
"""

import re
from typing import List, Optional

from inferra.indexer import CodeUnit
from .base import LanguageParser, register_parser


@register_parser
class JavaParser(LanguageParser):
    """Parser for Java source files."""

    LANGUAGE = "java"
    EXTENSIONS = [".java"]

    # Supports: class, interface, enum, record, sealed
    _CLASS_PATTERN = re.compile(
        r"^(?:public\s+|private\s+|protected\s+)?(?:abstract\s+)?(?:static\s+)?"
        r"(?:final\s+)?(?:sealed\s+)?(?:non-sealed\s+)?"
        r"(?:class|interface|enum|record)\s+(\w+)\s*(?:<[^>]*>)?"
        r"(?:\s*\([^)]*\))?"  # record parameters: record Foo(String bar)
        r"(?:\s+extends\s+(\w+)(?:\s*<[^>]*>)?)?"
        r"(?:\s+implements\s+([\w,\s<>]+))?"
        r"(?:\s+permits\s+([\w,\s]+))?",
        re.MULTILINE,
    )

    # Matches methods with or without access modifiers, handles:
    # - Generic return types: ResponseEntity<UserDTO>
    # - Nested generics: Map<String, List<Integer>>
    # - Annotation lines above (@Override, @Transactional)
    # - Package-private (no modifier)
    _METHOD_PATTERN = re.compile(
        r"^\s+(?:(?:public|private|protected)\s+)?"
        r"(?:static\s+)?(?:final\s+)?(?:synchronized\s+)?"
        r"(?:abstract\s+)?(?:default\s+)?"           # default for interface methods
        r"(?:<[\w\s,?]+>\s+)?"                        # method-level generics <T>
        r"([\w.]+(?:<[\w\s,.<>?]*>)?(?:\[\])?)\s+"    # return type (generics + arrays)
        r"(\w+)\s*\(([^)]*)\)",
        re.MULTILINE,
    )

    _ROUTE_PATTERNS = [
        # Spring: @GetMapping("/path"), @PostMapping("/path"), etc.
        re.compile(
            r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']?([^"\')\\s,]+)',
        ),
        # JAX-RS: @GET @Path("/path")
        re.compile(
            r'@(GET|POST|PUT|DELETE|PATCH)\s+.*@Path\s*\(\s*"([^"]+)"',
            re.DOTALL,
        ),
    ]

    _IMPORT_PATTERN = re.compile(r"^import\s+([\w.]+);", re.MULTILINE)

    _CALL_PATTERN = re.compile(r"\b(\w+)\s*\(")

    def parse(
        self,
        source: str,
        filepath: str,
        module_name: str,
    ) -> List[CodeUnit]:
        units = []
        lines = source.split("\n")
        imports = [m.group(1) for m in self._IMPORT_PATTERN.finditer(source)]
        route_map = self._extract_routes(source, lines)

        # Parse classes/interfaces/enums/records
        for match in self._CLASS_PATTERN.finditer(source):
            class_name = match.group(1)
            extends = match.group(2) or ""
            implements = match.group(3) or ""
            permits = match.group(4) or ""
            line_num = source[: match.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_num - 1)
            body = "\n".join(lines[line_num - 1 : end_line])

            # Detect kind from the match
            kind = "class"
            matched_text = match.group(0)
            for kw in ("interface", "enum", "record"):
                if kw in matched_text:
                    kind = kw
                    break

            sig_parts = [f"{kind} {class_name}"]
            if extends:
                sig_parts.append(f"extends {extends}")
            if implements:
                sig_parts.append(f"implements {implements.strip()}")
            if permits:
                sig_parts.append(f"permits {permits.strip()}")
            sig = " ".join(sig_parts)

            units.append(
                CodeUnit(
                    name=class_name,
                    qualified_name=f"{module_name}.{class_name}",
                    unit_type="class",
                    source_file=filepath,
                    start_line=line_num,
                    end_line=end_line,
                    signature=sig,
                    docstring=self._extract_javadoc(lines, line_num - 1),
                    body_text=body,
                    log_patterns=self._extract_log_patterns(body),
                    imports=imports,
                    calls=self._extract_calls(body),
                    tokens=self._tokenize(f"{class_name} {sig} {body}"),
                )
            )

        # Parse methods
        for match in self._METHOD_PATTERN.finditer(source):
            return_type = match.group(1)
            method_name = match.group(2)
            args = match.group(3)
            line_num = source[: match.start()].count("\n") + 1

            # Skip if the "method" is actually a class/interface/enum/record declaration
            if return_type in ("class", "interface", "enum", "record"):
                continue

            end_line = self._find_block_end(lines, line_num - 1)
            body = "\n".join(lines[line_num - 1 : end_line])
            sig = f"{return_type} {method_name}({args})"
            route = route_map.get(method_name)

            # Find enclosing class
            enclosing_class = self._find_enclosing_class(source, match.start())
            qname = f"{module_name}.{enclosing_class}.{method_name}" if enclosing_class else f"{module_name}.{method_name}"

            units.append(
                CodeUnit(
                    name=method_name,
                    qualified_name=qname,
                    unit_type="method",
                    source_file=filepath,
                    start_line=line_num,
                    end_line=end_line,
                    signature=sig,
                    docstring=self._extract_javadoc(lines, line_num - 1),
                    body_text=body,
                    log_patterns=self._extract_log_patterns(body),
                    imports=imports,
                    calls=self._extract_calls(body),
                    tokens=self._tokenize(f"{method_name} {sig} {body}"),
                    route_path=route,
                )
            )

        return units

    def _extract_routes(self, source: str, lines: List[str]) -> dict:
        """Map method names to route paths from Spring/JAX-RS annotations."""
        routes = {}
        for pattern in self._ROUTE_PATTERNS:
            for m in pattern.finditer(source):
                method = m.group(1).upper()
                if method == "REQUEST":
                    method = "ANY"
                path = m.group(2)
                # Find the method name on the next non-annotation line
                line_num = source[: m.end()].count("\n")
                for i in range(line_num, min(line_num + 5, len(lines))):
                    method_match = self._METHOD_PATTERN.search(lines[i])
                    if method_match:
                        routes[method_match.group(2)] = f"{method} {path}"
                        break
        return routes

    def _extract_calls(self, body: str) -> List[str]:
        calls = set()
        for m in self._CALL_PATTERN.finditer(body):
            name = m.group(1)
            if name not in ("if", "for", "while", "switch", "catch", "return",
                           "new", "throw", "class", "interface", "enum",
                           "System", "String", "Integer", "Boolean"):
                calls.add(name)
        return list(calls)

    def _extract_javadoc(self, lines: List[str], func_line: int) -> Optional[str]:
        """Extract JavaDoc comment above a method."""
        above = func_line - 1
        # Skip annotation lines
        while above >= 0 and lines[above].strip().startswith("@"):
            above -= 1
        if above >= 0 and lines[above].strip().endswith("*/"):
            doc_lines = []
            while above >= 0:
                doc_lines.insert(0, lines[above].strip())
                if lines[above].strip().startswith("/**"):
                    break
                above -= 1
            return "\n".join(doc_lines)
        return None

    def _find_enclosing_class(self, source: str, pos: int) -> Optional[str]:
        """Find the class name that contains a given source position."""
        best = None
        for m in self._CLASS_PATTERN.finditer(source):
            if m.start() < pos:
                best = m.group(1)
        return best

    def _find_block_end(self, lines: List[str], start: int) -> int:
        """Find the end of a braced block, ignoring braces in strings/comments."""
        depth = 0
        found_open = False
        in_string = False
        string_char = None
        in_line_comment = False
        in_block_comment = False

        for i in range(start, min(start + 1000, len(lines))):
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
                    if ch == "\\" and j + 1 < len(line):
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

                if ch in ('"', "'"):
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

        return min(start + 30, len(lines))
