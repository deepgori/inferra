"""
indexer.py — Multi-Stack Codebase Indexer

Indexes a full-stack codebase into a searchable store for RAG retrieval.
Supports:
- Python files (AST parsing): functions, classes, log patterns, imports
- SQL files (regex parsing): dbt models, CTEs, table refs, aggregations
- Config files (YAML, .env, TOML): connections, services, credentials

The index enables the RAG pipeline to answer: "Given this telemetry event,
which source code location most likely produced it?"

Supports two search backends (auto-selected):
- TF-IDF: keyword-based, always available
- Embeddings: semantic search via SVD, sentence-transformers, or OpenAI
  (auto-selects best available backend, falls back gracefully)
"""

import ast
import logging
import os
import re
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

@dataclass
class CodeUnit:
    """A single indexed unit of code — a function, class, or module-level block."""

    name: str
    qualified_name: str  # module.class.function
    unit_type: str  # "function", "class", "module", "method"
    source_file: str
    start_line: int
    end_line: int
    signature: str
    docstring: Optional[str]
    body_text: str  # full source text of the unit
    log_patterns: List[str]  # extracted log/print patterns
    imports: List[str]
    calls: List[str]  # functions this unit calls
    tokens: List[str] = field(default_factory=list)  # tokenized for search
    route_path: Optional[str] = None  # HTTP route from decorator, e.g. "GET /places"

    def __repr__(self) -> str:
        return (
            f"CodeUnit({self.unit_type} {self.qualified_name} "
            f"at {self.source_file}:{self.start_line}-{self.end_line})"
        )


@dataclass
class SearchResult:
    """A ranked result from the code index."""

    code_unit: CodeUnit
    score: float
    matched_terms: List[str]

    def __repr__(self) -> str:
        return (
            f"SearchResult(score={self.score:.3f}, "
            f"{self.code_unit.qualified_name} "
            f"at {self.code_unit.source_file}:{self.code_unit.start_line})"
        )


class CodeIndexer:
    """
    Indexes a Python codebase for RAG-based code retrieval.

    Usage:
        indexer = CodeIndexer()
        indexer.index_directory("/path/to/my/project")

        # Search by telemetry signal
        results = indexer.search("connection timeout database")

        # Search by log pattern
        results = indexer.search_by_log_pattern("Service unavailable")

        # Get stats
        print(indexer.stats())
    """

    def __init__(self, embedding_backend=None):
        self._units: List[CodeUnit] = []
        self._file_map: Dict[str, List[CodeUnit]] = defaultdict(list)
        self._log_index: Dict[str, List[CodeUnit]] = defaultdict(list)

        # TF-IDF components
        self._df: Dict[str, int] = defaultdict(int)  # document frequency
        self._tf: Dict[int, Dict[str, float]] = {}   # term frequency per doc
        self._indexed = False

        # Embedding components (optional — auto-detected if not provided)
        self._embedding_backend = embedding_backend
        self._vector_store = None
        self._embeddings_built = False

    @property
    def units(self) -> List[CodeUnit]:
        return self._units

    def index_directory(
        self,
        directory: str,
        exclude_patterns: Optional[List[str]] = None,
    ) -> "CodeIndexer":
        """
        Index all supported files in a directory.

        Supports: Python (AST), JavaScript/TypeScript, Go, Java (regex),
        SQL (regex), Config files (YAML, .env, TOML).

        Args:
            directory: Path to the codebase root
            exclude_patterns: Glob patterns to exclude (e.g., ["test_*", "__pycache__"])
        """
        exclude = set(exclude_patterns or ["__pycache__", ".git", "venv", ".venv", "node_modules"])
        root = Path(directory)

        # Phase 1: Index Python files (AST parsing)
        for py_file in root.rglob("*.py"):
            if any(ex in str(py_file) for ex in exclude):
                continue
            try:
                self._index_file(str(py_file), str(root))
            except (SyntaxError, UnicodeDecodeError):
                continue

        # Phase 1.5: Resolve router prefixes from include_router() calls
        self._resolve_router_prefixes(str(root), exclude)

        # Phase 1.6: Index JS/TS, Go, Java files (multi-language parsers)
        try:
            from .parsers import get_parser_for_file, SUPPORTED_EXTENSIONS
            multi_lang_extensions = list(SUPPORTED_EXTENSIONS.keys())
            multi_lang_counts = {}

            for ext in multi_lang_extensions:
                pattern = f"*{ext}"
                for src_file in root.rglob(pattern):
                    if any(ex in str(src_file) for ex in exclude):
                        continue
                    try:
                        parser = get_parser_for_file(str(src_file))
                        if not parser:
                            continue
                        with open(src_file, "r", encoding="utf-8", errors="ignore") as f:
                            source = f.read()
                        rel_path = os.path.relpath(str(src_file), str(root))
                        module_name = rel_path.replace("/", ".").replace("\\", ".")
                        # Strip extension from module name
                        for e in multi_lang_extensions:
                            if module_name.endswith(e):
                                module_name = module_name[: -len(e)]
                                break

                        units = parser.parse(source, str(src_file), module_name)
                        for unit in units:
                            self._units.append(unit)
                            self._file_map[str(src_file)].append(unit)
                            for pattern_str in unit.log_patterns:
                                self._log_index[pattern_str.lower()].append(unit)

                        lang = parser.LANGUAGE
                        multi_lang_counts[lang] = multi_lang_counts.get(lang, 0) + len(units)
                    except Exception:
                        continue

            if multi_lang_counts:
                lang_summary = ", ".join(f"{c} {l}" for l, c in multi_lang_counts.items())
                log.info(f"  Multi-language: {lang_summary} units")
        except ImportError:
            pass

        # Phase 2: Index SQL files (regex parsing)
        try:
            from .sql_indexer import SQLIndexer
            sql_indexer = SQLIndexer()
            sql_units = sql_indexer.index_directory(str(root), list(exclude))
            for unit in sql_units:
                self._units.append(unit)
                self._file_map[unit.source_file].append(unit)
        except ImportError:
            pass

        # Phase 3: Index config files (YAML, .env, TOML)
        try:
            from .config_indexer import ConfigIndexer
            config_indexer = ConfigIndexer()
            config_units = config_indexer.index_directory(str(root), list(exclude))
            for unit in config_units:
                self._units.append(unit)
                self._file_map[unit.source_file].append(unit)
        except ImportError:
            pass

        self._build_tfidf_index()
        self._build_embedding_index()
        return self


    def _resolve_router_prefixes(self, root: str, exclude: set):
        """
        Parse include_router() calls to resolve route prefixes.

        Builds a graph: caller_file --(prefix)--> target_file
        Then walks from root to accumulate full prefixes per file.
        """
        # Step 1: Parse include_router() calls → (caller_file, target_module_name, prefix)
        edges = []  # (caller_file, target_name, prefix)

        for py_file in Path(root).rglob("*.py"):
            if any(ex in str(py_file) for ex in exclude):
                continue
            try:
                with open(py_file, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
                tree = ast.parse(source, filename=str(py_file))
            except (SyntaxError, UnicodeDecodeError):
                continue

            # Build import map: local_name → full_module_path
            import_map = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        local = alias.asname or alias.name
                        import_map[local] = f"{node.module}.{alias.name}"

            # Find include_router calls
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = getattr(node, 'func', None)
                if not isinstance(func, ast.Attribute) or func.attr != "include_router":
                    continue

                # Extract prefix keyword
                prefix = ""
                for kw in node.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        prefix = kw.value.value

                # Resolve the target router's module
                if not node.args:
                    continue
                arg = node.args[0]
                target = None
                if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                    # articles.router → look up "articles" in imports
                    target = import_map.get(arg.value.id, arg.value.id)
                elif isinstance(arg, ast.Name):
                    # plain "router" → look up in imports
                    target = import_map.get(arg.id, arg.id)

                if target and prefix:
                    edges.append((str(py_file), target, prefix))

        # Step 2: Resolve module names → source file paths
        # e.g., "app.api.routes.articles" → "/path/to/api/routes/articles.py"
        file_edges = []  # (caller_file, target_file, prefix)
        all_files = list(set(u.source_file for u in self._units))
        for caller, target_module, prefix in edges:
            # Strip ".router" suffix if present (from articles.router)
            if target_module.endswith(".router"):
                target_module = target_module[:-7]
            parts = target_module.split(".")

            # Find best matching file (longest suffix wins to avoid ambiguity)
            best_match = None
            best_depth = 0
            for filepath in all_files:
                norm = filepath.replace("\\", "/")
                for i in range(len(parts)):
                    suffix = "/".join(parts[i:]) + ".py"
                    depth = len(parts) - i  # More parts matched = better
                    if norm.endswith(suffix) and depth > best_depth:
                        best_match = filepath
                        best_depth = depth
            if best_match:
                file_edges.append((caller, best_match, prefix))

        # Step 3: Accumulate prefixes per target file
        # Walk edges: if A includes B with /api, and B includes C with /articles,
        # then C gets /api/articles
        prefix_map = {}  # target_file → accumulated prefix
        # Direct prefixes first
        for caller, target, prefix in file_edges:
            if target not in prefix_map:
                prefix_map[target] = prefix
            # Don't overwrite — first match wins

        # Chain: if caller itself has a prefix, prepend it
        changed = True
        iterations = 0
        while changed and iterations < 5:
            changed = False
            iterations += 1
            for caller, target, prefix in file_edges:
                caller_prefix = prefix_map.get(caller, "")
                if caller_prefix:
                    full = caller_prefix.rstrip("/") + prefix
                    if prefix_map.get(target) != full:
                        prefix_map[target] = full
                        changed = True

        # Step 4: Apply prefixes to route_paths
        for unit in self._units:
            if not unit.route_path:
                continue
            file_prefix = prefix_map.get(unit.source_file, "")
            if file_prefix:
                method, path = unit.route_path.split(" ", 1)
                if path.strip():
                    full_path = file_prefix.rstrip("/") + "/" + path.lstrip("/")
                else:
                    full_path = file_prefix
                unit.route_path = f"{method} {full_path}"

    def index_file(self, filepath: str, root: str = "") -> "CodeIndexer":
        """Index a single Python file."""
        self._index_file(filepath, root)
        self._build_tfidf_index()
        self._build_embedding_index()
        return self

    def _index_file(self, filepath: str, root: str = ""):
        """Parse and index a single Python file."""
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()

        # Sanitize Jupyter/Colab magic commands (!pip install, %matplotlib, etc.)
        # so exported notebooks can be parsed by AST
        sanitized = self._sanitize_jupyter_source(source)

        try:
            tree = ast.parse(sanitized, filename=filepath)
        except SyntaxError:
            return

        lines = source.split("\n")
        rel_path = os.path.relpath(filepath, root) if root else filepath
        module_name = rel_path.replace("/", ".").replace("\\", ".").rstrip(".py")

        # Extract module-level imports
        module_imports = self._extract_imports(tree)

        # Index functions and classes
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                unit = self._parse_function(node, filepath, module_name, lines, module_imports)
                self._units.append(unit)
                self._file_map[filepath].append(unit)

                # Index log patterns
                for pattern in unit.log_patterns:
                    self._log_index[pattern.lower()].append(unit)

            elif isinstance(node, ast.ClassDef):
                unit = self._parse_class(node, filepath, module_name, lines, module_imports)
                self._units.append(unit)
                self._file_map[filepath].append(unit)

    @staticmethod
    def _sanitize_jupyter_source(source: str) -> str:
        """
        Strip Jupyter/Colab magic commands from Python source.

        Replaces lines starting with ! or % (shell commands, IPython magics)
        with blank comments to preserve line numbering for accurate source mapping.
        """
        sanitized_lines = []
        for line in source.split("\n"):
            stripped = line.lstrip()
            if stripped.startswith("!") or stripped.startswith("%"):
                # Replace with comment to preserve line numbers
                sanitized_lines.append("# [jupyter-magic] " + stripped)
            else:
                sanitized_lines.append(line)
        return "\n".join(sanitized_lines)

    def _parse_function(
        self,
        node: ast.FunctionDef,
        filepath: str,
        module: str,
        lines: List[str],
        imports: List[str],
    ) -> CodeUnit:
        """Extract a CodeUnit from a function/method AST node."""
        # Build signature
        args = []
        for arg in node.args.args:
            arg_name = arg.arg
            if arg.annotation:
                arg_name += f": {ast.unparse(arg.annotation)}"
            args.append(arg_name)

        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        sig = f"def {node.name}({', '.join(args)}){returns}"

        if isinstance(node, ast.AsyncFunctionDef):
            sig = "async " + sig

        # Extract HTTP route from decorators (FastAPI/Flask)
        route_path = self._extract_route_from_decorators(node)

        # Extract body text
        start = node.lineno - 1
        end = node.end_lineno if node.end_lineno else start + 1
        body_text = "\n".join(lines[start:end])

        # Extract docstring
        docstring = ast.get_docstring(node)

        # Extract log patterns
        log_patterns = self._extract_log_patterns(body_text)

        # Extract function calls
        calls = self._extract_calls(node)

        # Tokenize for search
        tokens = self._tokenize(f"{node.name} {sig} {docstring or ''} {body_text}")

        return CodeUnit(
            name=node.name,
            qualified_name=f"{module}.{node.name}",
            unit_type="async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
            source_file=filepath,
            start_line=node.lineno,
            end_line=end,
            signature=sig,
            docstring=docstring,
            body_text=body_text,
            log_patterns=log_patterns,
            imports=imports,
            calls=calls,
            tokens=tokens,
            route_path=route_path,
        )

    def _parse_class(
        self,
        node: ast.ClassDef,
        filepath: str,
        module: str,
        lines: List[str],
        imports: List[str],
    ) -> CodeUnit:
        """Extract a CodeUnit from a class AST node."""
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        sig = f"class {node.name}({bases})" if bases else f"class {node.name}"

        start = node.lineno - 1
        end = node.end_lineno if node.end_lineno else start + 1
        body_text = "\n".join(lines[start:end])

        docstring = ast.get_docstring(node)
        log_patterns = self._extract_log_patterns(body_text)
        calls = self._extract_calls(node)
        tokens = self._tokenize(f"{node.name} {sig} {docstring or ''} {body_text}")

        return CodeUnit(
            name=node.name,
            qualified_name=f"{module}.{node.name}",
            unit_type="class",
            source_file=filepath,
            start_line=node.lineno,
            end_line=end,
            signature=sig,
            docstring=docstring,
            body_text=body_text,
            log_patterns=log_patterns,
            imports=imports,
            calls=calls,
            tokens=tokens,
        )
    def _extract_route_from_decorators(self, node) -> Optional[str]:
        """
        Extract HTTP route from FastAPI/Flask decorators.

        Detects patterns like:
          @app.get("/places")
          @router.post("/api/v1/groups")
          @blueprint.route("/path", methods=["GET"])
        Returns: "GET /places" or None
        """
        HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call):
                func = decorator.func
                method = None
                path = None

                # @app.get("/path") or @router.post("/path")
                if isinstance(func, ast.Attribute) and func.attr in HTTP_METHODS:
                    method = func.attr.upper()
                    if decorator.args and isinstance(decorator.args[0], ast.Constant):
                        path = decorator.args[0].value

                # @app.route("/path", methods=["POST"])
                elif isinstance(func, ast.Attribute) and func.attr == "route":
                    if decorator.args and isinstance(decorator.args[0], ast.Constant):
                        path = decorator.args[0].value
                    method = "GET"  # default
                    for kw in decorator.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, ast.List):
                            if kw.value.elts and isinstance(kw.value.elts[0], ast.Constant):
                                method = kw.value.elts[0].value.upper()

                if method and path is not None:
                    return f"{method} {path}"
        return None

    def _extract_imports(self, tree: ast.Module) -> List[str]:
        """Extract import statements from a module."""
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imports.append(f"{module}.{alias.name}")
        return imports

    def _extract_log_patterns(self, source: str) -> List[str]:
        """Extract log/print statement patterns from source code."""
        patterns = []

        # Match logging.xxx("..."), print("..."), logger.xxx("...")
        log_re = re.compile(
            r'(?:logging|logger|log|print)\s*\.?\s*'
            r'(?:debug|info|warning|error|critical|exception|warn)?\s*'
            r'\(\s*[f]?["\'](.+?)["\']',
            re.IGNORECASE,
        )

        for match in log_re.finditer(source):
            patterns.append(match.group(1))

        # Also match bare print() calls
        print_re = re.compile(r'print\s*\(\s*[f]?["\'](.+?)["\']')
        for match in print_re.finditer(source):
            if match.group(1) not in patterns:
                patterns.append(match.group(1))

        return patterns

    def _extract_calls(self, node: ast.AST) -> List[str]:
        """Extract function call names from an AST node."""
        calls = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    calls.append(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    calls.append(child.func.attr)
        return calls

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text for TF-IDF indexing."""
        # Lowercase, split on non-alphanumeric, filter short tokens
        text = text.lower()
        tokens = re.findall(r'[a-z_][a-z0-9_]*', text)
        # Filter stopwords and very short tokens
        stopwords = {"self", "none", "true", "false", "return", "import", "from", "def", "class", "and", "or", "not", "the", "is", "in", "for", "if", "else", "with", "as"}
        return [t for t in tokens if len(t) > 2 and t not in stopwords]

    def _build_tfidf_index(self):
        """Build TF-IDF index from all indexed code units."""
        self._df.clear()
        self._tf.clear()

        n_docs = len(self._units)
        if n_docs == 0:
            return

        for i, unit in enumerate(self._units):
            # Term frequency
            tf = defaultdict(int)
            for token in unit.tokens:
                tf[token] += 1

            # Normalize TF
            max_tf = max(tf.values()) if tf else 1
            self._tf[i] = {t: count / max_tf for t, count in tf.items()}

            # Document frequency
            for token in set(unit.tokens):
                self._df[token] += 1

        self._indexed = True

    def _build_embedding_index(self):
        """Build the vector embedding index (if a backend is available)."""
        if not self._units:
            return

        # Auto-detect backend if not provided
        if self._embedding_backend is None:
            try:
                from inferra.embeddings import get_best_backend
                self._embedding_backend = get_best_backend()
            except ImportError:
                return

        if self._embedding_backend is None:
            return

        try:
            from inferra.embeddings import LocalEmbedding, VectorStore

            # For LocalEmbedding, we need to fit first
            if isinstance(self._embedding_backend, LocalEmbedding):
                documents = [unit.tokens for unit in self._units]
                self._embedding_backend.fit(documents)

            # Build text representations for embedding
            texts = []
            for unit in self._units:
                text = f"{unit.name} {unit.signature} {unit.docstring or ''} {unit.body_text[:500]}"
                texts.append(text)

            # Encode all code units
            vectors = self._embedding_backend.encode(texts)

            # Store in vector store
            self._vector_store = VectorStore()
            self._vector_store.add(vectors, list(range(len(self._units))))
            self._embeddings_built = True

        except (RuntimeError, ValueError, ImportError) as e:
            # Embedding build can fail (bad SVD, missing deps) — TF-IDF still works
            log.debug("Embedding build skipped: %s", e)
            self._embeddings_built = False

    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        """
        Search the code index using the best available method.

        If embeddings are available, uses reciprocal rank fusion of
        TF-IDF + semantic search for best results. Otherwise falls
        back to TF-IDF only.

        Args:
            query: Natural language or keyword query
            top_k: Number of results to return

        Returns:
            Ranked list of SearchResults
        """
        tfidf_results = self._search_tfidf(query, top_k=top_k * 2)

        if self._embeddings_built:
            semantic_results = self.search_semantic(query, top_k=top_k * 2)
            return self._fuse_results(tfidf_results, semantic_results, top_k)

        return tfidf_results[:top_k]

    def _search_tfidf(self, query: str, top_k: int = 5) -> List[SearchResult]:
        """Search using TF-IDF similarity only."""
        if not self._indexed or not self._units:
            return []

        query_tokens = self._tokenize(query)
        n_docs = len(self._units)
        scores = []

        for i, unit in enumerate(self._units):
            score = 0.0
            matched_terms = []

            for token in query_tokens:
                if token in self._tf.get(i, {}):
                    tf = self._tf[i][token]
                    idf = math.log(n_docs / (1 + self._df.get(token, 0)))
                    score += tf * idf
                    matched_terms.append(token)

            if score > 0:
                scores.append(SearchResult(
                    code_unit=unit,
                    score=score,
                    matched_terms=matched_terms,
                ))

        scores.sort(key=lambda r: r.score, reverse=True)
        return scores[:top_k]

    def search_semantic(self, query: str, top_k: int = 5) -> List[SearchResult]:
        """
        Search using vector embeddings (semantic similarity).

        Returns results ranked by cosine similarity in embedding space.
        Only available when an embedding backend is active.
        """
        if not self._embeddings_built or self._vector_store is None:
            return []

        query_vec = self._embedding_backend.encode_query(query)
        hits = self._vector_store.search(query_vec, top_k=top_k)

        results = []
        for unit_idx, score in hits:
            if 0 <= unit_idx < len(self._units):
                results.append(SearchResult(
                    code_unit=self._units[unit_idx],
                    score=score,
                    matched_terms=["[semantic]"],
                ))

        return results

    def _fuse_results(
        self,
        tfidf_results: List[SearchResult],
        semantic_results: List[SearchResult],
        top_k: int,
    ) -> List[SearchResult]:
        """
        Reciprocal Rank Fusion (RRF) to combine TF-IDF and semantic results.

        RRF(d) = sum(1 / (k + rank_i(d))) for each ranking system i.
        This is the standard technique used in RAG pipelines.
        """
        k = 60  # standard RRF constant
        scores: Dict[str, float] = defaultdict(float)
        result_map: Dict[str, SearchResult] = {}

        for rank, result in enumerate(tfidf_results):
            key = result.code_unit.qualified_name
            scores[key] += 1.0 / (k + rank + 1)
            if key not in result_map:
                result_map[key] = result

        for rank, result in enumerate(semantic_results):
            key = result.code_unit.qualified_name
            scores[key] += 1.0 / (k + rank + 1)
            if key not in result_map:
                result_map[key] = result

        # Sort by fused score
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        fused = []
        for name, score in ranked[:top_k]:
            if name in result_map:
                r = result_map[name]
                fused.append(SearchResult(
                    code_unit=r.code_unit,
                    score=score,
                    matched_terms=r.matched_terms,
                ))

        return fused

    def search_by_log_pattern(self, pattern: str, top_k: int = 5) -> List[SearchResult]:
        """
        Search for code units that produce a specific log pattern.

        This is the code-origin mapping feature: given a log line,
        find the source code that produced it.
        """
        pattern_lower = pattern.lower()
        results = []

        for unit in self._units:
            for log_pat in unit.log_patterns:
                if pattern_lower in log_pat.lower() or log_pat.lower() in pattern_lower:
                    # Score by pattern similarity (simple overlap)
                    overlap = len(set(pattern_lower.split()) & set(log_pat.lower().split()))
                    score = overlap / max(len(pattern_lower.split()), 1)
                    results.append(SearchResult(
                        code_unit=unit,
                        score=max(score, 0.5),  # minimum score for pattern match
                        matched_terms=[log_pat],
                    ))
                    break  # one match per unit is enough

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def search_by_function_name(self, name: str) -> Optional[CodeUnit]:
        """Find a code unit by exact function/class name."""
        for unit in self._units:
            if unit.name == name or unit.qualified_name.endswith(f".{name}"):
                return unit
        return None

    def search_by_route(self, span_name: str) -> Optional[CodeUnit]:
        """
        Find a code unit by matching its HTTP route to an OTel span name.

        OTel auto-instrumentation names spans like:
          "GET /places", "POST /api/v1/groups"
        We match these against routes extracted from decorators like:
          @app.get("/places"), @router.post("/groups")
        """
        # Normalize: "GET /api/v1/groups" → method="GET", path parts
        parts = span_name.split(" ", 1)
        if len(parts) != 2:
            return None
        method, request_path = parts[0].upper(), parts[1]

        for unit in self._units:
            if not unit.route_path:
                continue
            route_method, route_pattern = unit.route_path.split(" ", 1)
            if route_method != method:
                continue
            # Exact match
            if request_path == route_pattern:
                return unit
            # Match with prefix stripped: /api/v1/groups vs /groups
            if request_path.endswith(route_pattern):
                return unit
            # Match with path params: /hotels/{city} matches /hotels/mumbai
            if self._route_matches(route_pattern, request_path):
                return unit
        return None

    @staticmethod
    def _route_matches(pattern: str, path: str) -> bool:
        """Check if a route pattern (with {params}) matches a request path."""
        pattern_parts = pattern.strip("/").split("/")
        path_parts = path.strip("/").split("/")
        if len(pattern_parts) != len(path_parts):
            return False
        for pp, rp in zip(pattern_parts, path_parts):
            if pp.startswith("{") and pp.endswith("}"):
                continue  # Path parameter — matches anything
            if pp != rp:
                return False
        return True

    def get_file_units(self, filepath: str) -> List[CodeUnit]:
        """Get all code units in a specific file."""
        return self._file_map.get(filepath, [])

    # ------------------------------------------------------------------
    # v0.5.0 — Advanced correlation search methods
    # ------------------------------------------------------------------

    def search_by_body_pattern(self, pattern: str, max_results: int = 3) -> List[CodeUnit]:
        """Find code units whose body contains a string pattern.

        Useful for matching DB queries (e.g. 'products.aggregate') or
        library calls (e.g. 'redis.get') to the functions that use them.
        """
        pattern_lower = pattern.lower()
        results = []
        for unit in self._units:
            if "function" in unit.unit_type or unit.unit_type == "method":
                body = unit.body_text.lower()
                if pattern_lower in body:
                    results.append(unit)
                    if len(results) >= max_results:
                        break
        return results

    def search_by_filepath_keyword(self, keyword: str) -> List[CodeUnit]:
        """Return all code units from files whose path contains keyword.

        Useful for service-name → directory mapping: if the span's service
        is 'product-service', search for units in files containing 'product'.
        """
        keyword_lower = keyword.lower()
        results = []
        for fpath, units in self._file_map.items():
            if keyword_lower in fpath.lower():
                results.extend(units)
        return results

    def search_by_route_fuzzy(self, path: str) -> Optional[CodeUnit]:
        """Match a request path to a route after stripping numeric IDs.

        '/api/products/12345' → '/api/products/{id}'
        '/users/42/orders'    → '/users/{id}/orders'
        """
        import re
        # Replace numeric path segments with {id} placeholder
        normalized = re.sub(r'/\d+', '/{id}', path)
        # Also try with MongoDB-style hex IDs
        normalized2 = re.sub(r'/[0-9a-f]{24}', '/{id}', path)

        for unit in self._units:
            if not unit.route_path:
                continue
            _, route_pattern = unit.route_path.split(" ", 1) if " " in unit.route_path else ("", unit.route_path)
            if route_pattern == normalized or route_pattern == normalized2:
                return unit
            if normalized.endswith(route_pattern) or normalized2.endswith(route_pattern):
                return unit
            # Also try the route_matches helper for param patterns
            if self._route_matches(route_pattern, normalized):
                return unit
        return None

    def search_functions_by_keywords(self, keywords: List[str], max_results: int = 3) -> List[CodeUnit]:
        """Find functions matching multiple keywords by relevance score.

        Decomposes span names like 'mongodb.products.aggregate' into
        ['mongodb', 'products', 'aggregate'] and scores each code unit
        by how many keywords appear in its name, body, or file path.
        """
        keywords_lower = [k.lower() for k in keywords if len(k) > 2]
        if not keywords_lower:
            return []

        scored = []
        for unit in self._units:
            if unit.unit_type not in ("function", "method", "async_function"):
                continue
            score = 0
            name_lower = unit.name.lower()
            qual_lower = unit.qualified_name.lower()
            body_lower = unit.body_text[:2000].lower()
            path_lower = unit.source_file.lower()

            for kw in keywords_lower:
                if kw in name_lower:
                    score += 3  # Name match is strongest signal
                elif kw in qual_lower:
                    score += 2
                elif kw in path_lower:
                    score += 1.5
                elif kw in body_lower:
                    score += 1

            if score > 0:
                scored.append((score, unit))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [unit for _, unit in scored[:max_results]]

    def stats(self) -> dict:
        """Get indexing statistics."""
        return {
            "total_units": len(self._units),
            "files_indexed": len(self._file_map),
            "functions": sum(1 for u in self._units if "function" in u.unit_type),
            "classes": sum(1 for u in self._units if u.unit_type == "class"),
            "sql_models": sum(1 for u in self._units if u.unit_type == "sql_model"),
            "config_entries": sum(1 for u in self._units if u.unit_type == "config"),
            "log_patterns": sum(len(u.log_patterns) for u in self._units),
            "unique_tokens": len(self._df),
        }
