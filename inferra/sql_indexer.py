"""
sql_indexer.py — SQL and dbt Model Indexer

Parses SQL files (including dbt models with Jinja) to extract:
- Table references (FROM, JOIN)
- dbt refs and sources: {{ ref('model') }}, {{ source('schema', 'table') }}
- CTEs (WITH ... AS)
- Column names from SELECT clauses
- Aggregations (SUM, COUNT, AVG, etc.)
- WHERE/HAVING filter conditions
- dbt macros ({{ dbt_utils.* }})

Outputs CodeUnit objects compatible with the main CodeIndexer.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .indexer import CodeUnit


# ── Regex patterns ────────────────────────────────────────────────────────────

# dbt-specific patterns
_DBT_REF = re.compile(r"\{\{\s*ref\s*\(\s*['\"](\w+)['\"]\s*\)\s*\}\}")
_DBT_SOURCE = re.compile(
    r"\{\{\s*source\s*\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]\s*\)\s*\}\}"
)
_DBT_MACRO = re.compile(r"\{\{\s*([\w.]+)\s*\(")

# Standard SQL patterns (case-insensitive)
_CTE = re.compile(r"\b(\w+)\s+AS\s*\(", re.IGNORECASE)
_FROM_TABLE = re.compile(r"\bFROM\s+(\w+(?:\.\w+)*)", re.IGNORECASE)
_JOIN_TABLE = re.compile(r"\bJOIN\s+(\w+(?:\.\w+)*)", re.IGNORECASE)
_SELECT_COLS = re.compile(
    r"\bSELECT\s+(.*?)(?:\bFROM\b|\bINTO\b|\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|\bUNION\b|;|\))",
    re.IGNORECASE | re.DOTALL,
)
_AGGREGATION = re.compile(
    r"\b(SUM|COUNT|AVG|MIN|MAX|STDDEV|VARIANCE)\s*\(",
    re.IGNORECASE,
)
_WHERE_CLAUSE = re.compile(
    r"\bWHERE\s+(.*?)(?:\bGROUP\b|\bORDER\b|\bLIMIT\b|\bHAVING\b|\bUNION\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)
_GROUP_BY = re.compile(r"\bGROUP\s+BY\s+(.*?)(?:\bORDER\b|\bHAVING\b|\bLIMIT\b|;|$)", re.IGNORECASE | re.DOTALL)
_HAVING = re.compile(r"\bHAVING\s+(.*?)(?:\bORDER\b|\bLIMIT\b|;|$)", re.IGNORECASE | re.DOTALL)


# ── SQL Model Metadata ───────────────────────────────────────────────────────

class SQLModel:
    """Parsed metadata from a single SQL file."""

    def __init__(self, filepath: str, content: str):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.name = os.path.splitext(self.filename)[0]
        self.content = content
        self.lines = content.split("\n")

        # Extracted elements
        self.dbt_refs: List[str] = []
        self.dbt_sources: List[Tuple[str, str]] = []
        self.dbt_macros: List[str] = []
        self.tables: List[str] = []
        self.ctes: List[str] = []
        self.columns: List[str] = []
        self.aggregations: List[str] = []
        self.filters: List[str] = []
        self.group_by_cols: List[str] = []

        self._parse()

    def _parse(self):
        """Extract all SQL elements from the content."""
        text = self.content

        # dbt refs: {{ ref('model_name') }}
        self.dbt_refs = _DBT_REF.findall(text)

        # dbt sources: {{ source('schema', 'table') }}
        self.dbt_sources = _DBT_SOURCE.findall(text)

        # dbt macros: {{ dbt_utils.generate_surrogate_key(...) }}
        raw_macros = _DBT_MACRO.findall(text)
        # Filter out ref/source which we handle separately
        self.dbt_macros = [m for m in raw_macros if m not in ("ref", "source")]

        # CTEs: WITH name AS (...)
        self.ctes = _CTE.findall(text)

        # FROM/JOIN tables (skip dbt template refs and CTEs)
        dbt_placeholders = set(self.ctes)
        raw_tables = _FROM_TABLE.findall(text) + _JOIN_TABLE.findall(text)
        self.tables = [
            t for t in raw_tables
            if t.lower() not in dbt_placeholders
            and not t.startswith("{{")
            and t.lower() not in ("select", "where", "set", "values", "null")
        ]

        # Columns from SELECT
        for match in _SELECT_COLS.findall(text):
            cols = self._parse_columns(match)
            self.columns.extend(cols)

        # Aggregations
        self.aggregations = list(set(
            agg.upper() for agg in _AGGREGATION.findall(text)
        ))

        # WHERE filters
        for match in _WHERE_CLAUSE.findall(text):
            clause = match.strip()
            if clause:
                self.filters.append(clause[:200])  # Cap length

        # GROUP BY
        for match in _GROUP_BY.findall(text):
            cols = [c.strip() for c in match.split(",") if c.strip()]
            self.group_by_cols.extend(cols[:20])

    def _parse_columns(self, select_body: str) -> List[str]:
        """Extract column names/aliases from a SELECT clause body."""
        columns = []
        # Split by comma, handling nested parens
        depth = 0
        current = []
        for char in select_body:
            if char == "(":
                depth += 1
                current.append(char)
            elif char == ")":
                depth -= 1
                current.append(char)
            elif char == "," and depth == 0:
                columns.append("".join(current).strip())
                current = []
            else:
                current.append(char)
        if current:
            columns.append("".join(current).strip())

        # Extract aliases (last word, or AS alias)
        result = []
        alias_re = re.compile(r"\bAS\s+(\w+)\s*$", re.IGNORECASE)
        for col in columns:
            col = col.strip()
            if not col or col == "*":
                continue
            # Check for AS alias
            alias_match = alias_re.search(col)
            if alias_match:
                result.append(alias_match.group(1))
            else:
                # Last word (might be table.column or just column)
                parts = col.split()
                if parts:
                    last = parts[-1].strip(",").strip()
                    # Handle table.column format
                    if "." in last:
                        last = last.split(".")[-1]
                    if last and re.match(r"^\w+$", last):
                        result.append(last)
        return result

    @property
    def dependencies(self) -> List[str]:
        """All upstream dependencies (refs + sources)."""
        deps = list(self.dbt_refs)
        deps.extend(f"{schema}.{table}" for schema, table in self.dbt_sources)
        deps.extend(self.tables)
        return deps

    @property
    def signature(self) -> str:
        """Human-readable signature of this SQL model."""
        parts = [f"SQL model: {self.name}"]
        if self.dbt_refs:
            parts.append(f"refs: {', '.join(self.dbt_refs)}")
        if self.dbt_sources:
            parts.append(f"sources: {', '.join(f'{s}.{t}' for s, t in self.dbt_sources)}")
        if self.ctes:
            parts.append(f"CTEs: {', '.join(self.ctes)}")
        if self.aggregations:
            parts.append(f"aggregations: {', '.join(self.aggregations)}")
        return " | ".join(parts)


# ── SQL Indexer ───────────────────────────────────────────────────────────────

class SQLIndexer:
    """
    Indexes SQL files in a codebase, outputting CodeUnit objects.

    Usage:
        sql_indexer = SQLIndexer()
        units = sql_indexer.index_directory("/path/to/dbt/project")

        for unit in units:
            print(unit.name, unit.unit_type, unit.tokens)
    """

    def index_directory(
        self,
        directory: str,
        exclude_patterns: Optional[List[str]] = None,
    ) -> List[CodeUnit]:
        """Index all SQL files in a directory tree."""
        exclude = set(exclude_patterns or [])
        root = Path(directory)
        units = []

        for sql_file in root.rglob("*.sql"):
            if any(ex in str(sql_file) for ex in exclude):
                continue
            try:
                file_units = self.index_file(str(sql_file), str(root))
                units.extend(file_units)
            except (UnicodeDecodeError, OSError):
                continue

        return units

    def index_file(self, filepath: str, root: str = "") -> List[CodeUnit]:
        """Parse a single SQL file and return CodeUnit(s)."""
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        if not content.strip():
            return []

        model = SQLModel(filepath, content)

        # Build tokens for search
        tokens = self._tokenize(model)

        # Build a descriptive body text
        body_parts = [content]
        if model.dbt_refs:
            body_parts.append(f"Dependencies: {', '.join(model.dbt_refs)}")
        if model.dbt_sources:
            body_parts.append(f"Sources: {', '.join(f'{s}.{t}' for s, t in model.dbt_sources)}")

        # Compute relative path for qualified name
        if root:
            try:
                rel_path = os.path.relpath(filepath, root)
            except ValueError:
                rel_path = filepath
        else:
            rel_path = filepath
        qualified = rel_path.replace(os.sep, ".").replace(".sql", "")

        unit = CodeUnit(
            name=model.name,
            qualified_name=qualified,
            unit_type="sql_model",
            source_file=filepath,
            start_line=1,
            end_line=len(model.lines),
            signature=model.signature,
            docstring=None,
            body_text="\n".join(body_parts),
            log_patterns=[],
            imports=model.dependencies,
            calls=model.dbt_macros,
            tokens=tokens,
        )

        return [unit]

    def _tokenize(self, model: SQLModel) -> List[str]:
        """Create searchable tokens from SQL model metadata."""
        tokens = []

        # Model name tokens
        tokens.extend(self._split_name(model.name))

        # Table/ref tokens
        for ref in model.dbt_refs:
            tokens.extend(self._split_name(ref))
            tokens.append(ref.lower())

        for schema, table in model.dbt_sources:
            tokens.extend([schema.lower(), table.lower()])
            tokens.extend(self._split_name(table))

        for table in model.tables:
            tokens.extend(self._split_name(table))
            tokens.append(table.lower())

        # Column tokens
        for col in model.columns:
            tokens.extend(self._split_name(col))
            tokens.append(col.lower())

        # CTE tokens
        for cte in model.ctes:
            tokens.extend(self._split_name(cte))

        # Aggregation tokens
        tokens.extend(agg.lower() for agg in model.aggregations)

        # Macro tokens
        for macro in model.dbt_macros:
            tokens.extend(macro.lower().split("."))

        # SQL-specific semantic tokens
        if model.aggregations:
            tokens.append("aggregation")
        if model.filters:
            tokens.append("filter")
            tokens.append("where")
        if model.ctes:
            tokens.append("cte")
            tokens.append("common_table_expression")
        if model.group_by_cols:
            tokens.append("group_by")
            tokens.append("grouping")
        if model.dbt_refs or model.dbt_sources:
            tokens.append("dbt")
            tokens.append("transformation")

        # Generic SQL tokens
        tokens.extend(["sql", "query", "model", "data"])

        return [t for t in tokens if t and len(t) > 1]

    def _split_name(self, name: str) -> List[str]:
        """Split a name like 'fct_invoices' into ['fct', 'invoices']."""
        # Split on underscores, camelCase boundaries, dots
        parts = re.split(r"[_.\-]", name)
        result = []
        for part in parts:
            # Also split camelCase
            sub = re.sub(r"([a-z])([A-Z])", r"\1_\2", part).lower().split("_")
            result.extend(s for s in sub if s)
        return result
