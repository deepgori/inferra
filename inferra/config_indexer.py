"""
config_indexer.py — Configuration File Indexer

Parses YAML, TOML, .env, and docker-compose files to extract:
- Connection strings (host, port, database, user)
- Service definitions (docker-compose services, ports, images)
- Environment variables
- dbt profiles (database type, project, dataset)
- Data source configurations (Soda, Airflow, etc.)

Outputs CodeUnit objects compatible with the main CodeIndexer,
enabling the RAG pipeline to correlate infrastructure errors
(e.g., "Connection refused on port 5432") back to config files.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

from .indexer import CodeUnit


# ── Config Parsers (lightweight, no external deps) ────────────────────────────

class _SimpleYAMLParser:
    """
    Minimal YAML parser for config files — handles the common subset
    needed for docker-compose, dbt profiles, and Soda configs.
    No external dependency required (no PyYAML).
    
    This handles: key: value, nested indentation, lists, multi-line strings.
    It does NOT handle advanced YAML features (anchors, complex merges, etc.)
    """

    @staticmethod
    def parse(text: str) -> Dict[str, Any]:
        """Parse a YAML string into a nested dict."""
        try:
            # Try PyYAML if available
            import yaml
            return yaml.safe_load(text) or {}
        except ImportError:
            pass
        except Exception:
            # Custom tags (!ENV, !include, etc.) crash safe_load — fall back
            pass

        # Fallback: simple indentation-based parser
        return _SimpleYAMLParser._parse_basic(text)

    @staticmethod
    def _parse_basic(text: str) -> Dict[str, Any]:
        """Basic YAML parser — handles key:value and indentation."""
        result = {}
        stack: List[Tuple[int, dict]] = [(-1, result)]

        for line in text.split("\n"):
            stripped = line.rstrip()
            if not stripped or stripped.startswith("#"):
                continue

            indent = len(line) - len(line.lstrip())
            content = stripped.lstrip()

            # Pop stack to find parent
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()

            parent = stack[-1][1]

            if ":" in content:
                key, _, value = content.partition(":")
                key = key.strip().strip("'\"")
                value = value.strip().strip("'\"")

                if value:
                    parent[key] = value
                else:
                    child = {}
                    parent[key] = child
                    stack.append((indent, child))
            elif content.startswith("- "):
                # List item — store as list in parent
                item = content[2:].strip().strip("'\"")
                # Find the last key that should contain this list
                if isinstance(parent, dict):
                    # Convert parent's last value to a list if needed
                    last_key = list(parent.keys())[-1] if parent else None
                    if last_key and isinstance(parent[last_key], dict) and not parent[last_key]:
                        parent[last_key] = [item]
                    elif last_key and isinstance(parent[last_key], list):
                        parent[last_key].append(item)

        return result


class _EnvParser:
    """Parse .env files into key-value pairs."""

    @staticmethod
    def parse(text: str) -> Dict[str, str]:
        result = {}
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                result[key] = value
        return result


# ── Config Element Extraction ─────────────────────────────────────────────────

class ConfigElement:
    """A single extracted configuration element."""

    def __init__(
        self,
        name: str,
        element_type: str,  # "service", "connection", "environment", "credential", "setting"
        properties: Dict[str, str],
        source_file: str,
        line_range: Tuple[int, int] = (1, 1),
    ):
        self.name = name
        self.element_type = element_type
        self.properties = properties
        self.source_file = source_file
        self.line_range = line_range

    @property
    def signature(self) -> str:
        parts = [f"config:{self.element_type} {self.name}"]
        key_props = {k: v for k, v in self.properties.items()
                     if k in ("type", "host", "port", "image", "database", "dataset", "project")}
        if key_props:
            parts.append(" | ".join(f"{k}={v}" for k, v in key_props.items()))
        return " | ".join(parts)

    @property
    def tokens(self) -> List[str]:
        """Searchable tokens for this config element."""
        toks = []
        # Name tokens
        toks.extend(re.split(r"[_.\-/]", self.name.lower()))
        # Type token
        toks.append(self.element_type)
        toks.append("config")
        toks.append("configuration")

        # Property value tokens
        for key, value in self.properties.items():
            toks.append(key.lower())
            # Split values into tokens
            if isinstance(value, str):
                toks.extend(re.split(r"[_.\-:/\s]", value.lower()))

        # Semantic tokens based on type
        if self.element_type == "service":
            toks.extend(["docker", "container", "service", "deployment"])
        elif self.element_type == "connection":
            toks.extend(["database", "connection", "host", "port"])
        elif self.element_type == "credential":
            toks.extend(["credential", "secret", "key", "auth", "authentication"])
        elif self.element_type == "environment":
            toks.extend(["environment", "variable", "env"])

        return [t for t in toks if t and len(t) > 1]


# ── Config Indexer ────────────────────────────────────────────────────────────

class ConfigIndexer:
    """
    Indexes configuration files in a codebase, outputting CodeUnit objects.

    Supported formats:
    - YAML (.yml, .yaml) — docker-compose, dbt profiles, Soda configs
    - .env files — environment variable definitions
    - TOML (.toml) — project configs (pyproject.toml, etc.)

    Usage:
        config_indexer = ConfigIndexer()
        units = config_indexer.index_directory("/path/to/project")

        for unit in units:
            print(unit.name, unit.unit_type, unit.tokens)
    """

    SUPPORTED_EXTENSIONS = {".yml", ".yaml", ".env", ".toml"}

    def index_directory(
        self,
        directory: str,
        exclude_patterns: Optional[List[str]] = None,
    ) -> List[CodeUnit]:
        """Index all config files in a directory tree."""
        exclude = set(exclude_patterns or [])
        root = Path(directory)
        units = []

        for config_file in self._find_config_files(root, exclude):
            try:
                file_units = self.index_file(str(config_file), str(root))
                units.extend(file_units)
            except (UnicodeDecodeError, OSError):
                continue

        return units

    def _find_config_files(self, root: Path, exclude: Set[str]):
        """Find all supported config files."""
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(ex in str(path) for ex in exclude):
                continue

            # Check extension
            if path.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                yield path
            # Also check .env files (might not have extension matching)
            elif path.name.startswith(".env"):
                yield path

    def index_file(self, filepath: str, root: str = "") -> List[CodeUnit]:
        """Parse a single config file and return CodeUnit(s)."""
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        if not content.strip():
            return []

        filename = os.path.basename(filepath)
        ext = os.path.splitext(filepath)[1].lower()

        elements: List[ConfigElement] = []

        if ext in (".yml", ".yaml"):
            elements = self._parse_yaml_config(filepath, content)
        elif filename.startswith(".env") or ext == ".env":
            elements = self._parse_env_file(filepath, content)
        elif ext == ".toml":
            elements = self._parse_toml_file(filepath, content)

        # Convert elements to CodeUnits
        return [self._element_to_code_unit(elem, root) for elem in elements]

    def _parse_yaml_config(self, filepath: str, content: str) -> List[ConfigElement]:
        """Extract config elements from a YAML file."""
        elements = []
        filename = os.path.basename(filepath)
        data = _SimpleYAMLParser.parse(content)
        lines = content.split("\n")

        if not isinstance(data, dict):
            return elements

        # Docker Compose detection
        if "services" in data and isinstance(data["services"], dict):
            for svc_name, svc_config in data["services"].items():
                props = {}
                if isinstance(svc_config, dict):
                    if "image" in svc_config:
                        props["image"] = str(svc_config["image"])
                    if "ports" in svc_config:
                        ports = svc_config["ports"]
                        if isinstance(ports, list):
                            props["ports"] = ", ".join(str(p) for p in ports)
                        else:
                            props["ports"] = str(ports)
                    if "environment" in svc_config:
                        env = svc_config["environment"]
                        if isinstance(env, dict):
                            props.update({k: str(v) for k, v in env.items()})
                        elif isinstance(env, list):
                            for item in env:
                                if "=" in str(item):
                                    k, _, v = str(item).partition("=")
                                    props[k.strip()] = v.strip()
                    if "restart" in svc_config:
                        props["restart"] = str(svc_config["restart"])
                    if "volumes" in svc_config and isinstance(svc_config["volumes"], list):
                        props["volumes"] = ", ".join(str(v) for v in svc_config["volumes"])

                elements.append(ConfigElement(
                    name=svc_name,
                    element_type="service",
                    properties=props,
                    source_file=filepath,
                ))

        # dbt profile detection
        if "outputs" in str(data):
            self._extract_dbt_profiles(data, filepath, elements)

        # Connection/data source detection
        for key, value in data.items():
            if isinstance(value, dict):
                # Check for connection-like properties
                conn_keys = {"host", "port", "database", "user", "password",
                             "type", "method", "project", "dataset", "connection"}
                if conn_keys & set(str(k).lower() for k in value.keys()):
                    props = {k: str(v) for k, v in value.items()
                             if not isinstance(v, (dict, list))}
                    elem_type = "connection"
                    if any(k in str(value).lower() for k in ["api_key", "secret", "password", "token"]):
                        elem_type = "credential"
                    elements.append(ConfigElement(
                        name=key,
                        element_type=elem_type,
                        properties=props,
                        source_file=filepath,
                    ))

                # Recurse one level for nested connections
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, dict):
                        sub_conn_keys = conn_keys & set(str(k).lower() for k in sub_value.keys())
                        if sub_conn_keys:
                            props = {k: str(v) for k, v in sub_value.items()
                                     if not isinstance(v, (dict, list))}
                            elem_type = "connection"
                            if any(k in str(sub_value).lower() for k in ["api_key", "secret", "password"]):
                                elem_type = "credential"
                            elements.append(ConfigElement(
                                name=f"{key}.{sub_key}",
                                element_type=elem_type,
                                properties=props,
                                source_file=filepath,
                            ))

        # Soda configuration detection
        if "data_source" in str(content).lower() or "soda_cloud" in str(data):
            self._extract_soda_config(data, filepath, elements)

        return elements

    def _extract_dbt_profiles(self, data: dict, filepath: str, elements: list):
        """Extract dbt profile connection info."""
        for profile_name, profile_data in data.items():
            if not isinstance(profile_data, dict):
                continue
            outputs = profile_data.get("outputs", {})
            if not isinstance(outputs, dict):
                continue
            for env_name, env_data in outputs.items():
                if isinstance(env_data, dict):
                    props = {k: str(v) for k, v in env_data.items()
                             if not isinstance(v, (dict, list))}
                    elements.append(ConfigElement(
                        name=f"dbt:{profile_name}.{env_name}",
                        element_type="connection",
                        properties=props,
                        source_file=filepath,
                    ))

    def _extract_soda_config(self, data: dict, filepath: str, elements: list):
        """Extract Soda data source connections."""
        if "soda_cloud" in data and isinstance(data["soda_cloud"], dict):
            props = {k: str(v) for k, v in data["soda_cloud"].items()
                     if not isinstance(v, (dict, list))}
            elements.append(ConfigElement(
                name="soda_cloud",
                element_type="credential",
                properties=props,
                source_file=filepath,
            ))

    def _parse_env_file(self, filepath: str, content: str) -> List[ConfigElement]:
        """Extract environment variables from a .env file."""
        elements = []
        env_vars = _EnvParser.parse(content)

        if not env_vars:
            return elements

        # Group by category
        conn_vars = {}
        secret_vars = {}
        other_vars = {}

        for key, value in env_vars.items():
            key_lower = key.lower()
            if any(k in key_lower for k in ["host", "port", "database", "db_", "url", "connection"]):
                conn_vars[key] = value
            elif any(k in key_lower for k in ["key", "secret", "password", "token", "auth"]):
                secret_vars[key] = value
            else:
                other_vars[key] = value

        if conn_vars:
            elements.append(ConfigElement(
                name="env:connections",
                element_type="connection",
                properties=conn_vars,
                source_file=filepath,
            ))
        if secret_vars:
            elements.append(ConfigElement(
                name="env:credentials",
                element_type="credential",
                properties=secret_vars,
                source_file=filepath,
            ))
        if other_vars:
            elements.append(ConfigElement(
                name="env:settings",
                element_type="environment",
                properties=other_vars,
                source_file=filepath,
            ))

        return elements

    def _parse_toml_file(self, filepath: str, content: str) -> List[ConfigElement]:
        """Extract config from TOML files (basic parser)."""
        elements = []
        current_section = "root"
        props: Dict[str, str] = {}

        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Section header
            if line.startswith("["):
                if props:
                    elements.append(ConfigElement(
                        name=f"toml:{current_section}",
                        element_type="setting",
                        properties=props,
                        source_file=filepath,
                    ))
                current_section = line.strip("[]").strip()
                props = {}
            elif "=" in line:
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip().strip("'\"")

        if props:
            elements.append(ConfigElement(
                name=f"toml:{current_section}",
                element_type="setting",
                properties=props,
                source_file=filepath,
            ))

        return elements

    def _element_to_code_unit(self, element: ConfigElement, root: str) -> CodeUnit:
        """Convert a ConfigElement to a CodeUnit for the search index."""
        # Build body text
        body_lines = [f"# {element.element_type}: {element.name}"]
        for key, value in element.properties.items():
            body_lines.append(f"{key}: {value}")

        # Relative path for qualified name
        if root:
            try:
                rel = os.path.relpath(element.source_file, root)
            except ValueError:
                rel = element.source_file
        else:
            rel = element.source_file
        qualified = f"{rel}:{element.name}"

        return CodeUnit(
            name=element.name,
            qualified_name=qualified,
            unit_type="config",
            source_file=element.source_file,
            start_line=element.line_range[0],
            end_line=element.line_range[1],
            signature=element.signature,
            docstring=None,
            body_text="\n".join(body_lines),
            log_patterns=[],
            imports=[],
            calls=[],
            tokens=element.tokens,
        )
