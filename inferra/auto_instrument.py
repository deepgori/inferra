"""
auto_instrument.py — Automatic Instrumentation Generator

Given an indexed codebase, generates a monkey-patch script that wraps
discovered functions with tracing decorators at import time.

Usage:
    python -m inferra.auto_instrument --project ./myapp --output instrument.py
    python instrument.py  # patches all discovered functions with @trace
"""

import logging
import os
from pathlib import Path
from typing import List, Optional

from inferra.indexer import CodeIndexer, CodeUnit

log = logging.getLogger(__name__)


def generate_instrumentation_script(
    indexer: CodeIndexer,
    output_path: str = "auto_instrument_patch.py",
    exclude_names: Optional[List[str]] = None,
    include_routes_only: bool = False,
) -> str:
    """
    Generate a Python script that monkey-patches functions with OTel tracing.

    Args:
        indexer: Indexed codebase
        output_path: Where to save the generated script
        exclude_names: Function names to skip
        include_routes_only: Only instrument route handlers

    Returns:
        Path to the generated script
    """
    exclude = set(exclude_names or [
        "__init__", "__repr__", "__str__", "__eq__", "__hash__",
        "__len__", "__getitem__", "__setitem__", "__contains__",
        "main", "setup", "configure", "create_app",
    ])

    # Collect functions to instrument
    targets = []
    for unit in indexer.units:
        if unit.unit_type not in ("function", "async_function", "method"):
            continue
        if unit.name in exclude or unit.name.startswith("_"):
            continue
        if include_routes_only and not unit.route_path:
            continue
        targets.append(unit)

    # Group by module
    module_groups = {}
    for unit in targets:
        # Convert file path to importable module
        rel = unit.source_file
        module = _filepath_to_module(rel)
        if module not in module_groups:
            module_groups[module] = []
        module_groups[module].append(unit)

    # Generate the script
    lines = [
        '"""',
        "Auto-generated instrumentation script by Inferra.",
        f"Instruments {len(targets)} functions across {len(module_groups)} modules.",
        "",
        "Usage:",
        "    python auto_instrument_patch.py  # then run your app",
        '"""',
        "",
        "import functools",
        "import time",
        "import logging",
        "",
        "try:",
        "    from opentelemetry import trace",
        "    from opentelemetry.sdk.trace import TracerProvider",
        "    from opentelemetry.sdk.trace.export import SimpleSpanProcessor",
        "    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter",
        "",
        '    provider = TracerProvider()',
        '    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))',
        '    trace.set_tracer_provider(provider)',
        '    _tracer = trace.get_tracer("inferra.auto_instrument")',
        '    _HAS_OTEL = True',
        "except ImportError:",
        '    _HAS_OTEL = False',
        "",
        "log = logging.getLogger('inferra.auto_instrument')",
        "",
        "",
        "def _wrap_function(module_name, func_name, original_func):",
        '    """Wrap a function with tracing."""',
        "    @functools.wraps(original_func)",
        "    def wrapper(*args, **kwargs):",
        "        if _HAS_OTEL:",
        '            with _tracer.start_as_current_span(',
        '                f"{module_name}.{func_name}",',
        '                attributes={"code.function": func_name, "code.namespace": module_name},',
        "            ):",
        "                return original_func(*args, **kwargs)",
        "        else:",
        "            return original_func(*args, **kwargs)",
        "    return wrapper",
        "",
        "",
        "def _wrap_async_function(module_name, func_name, original_func):",
        '    """Wrap an async function with tracing."""',
        "    @functools.wraps(original_func)",
        "    async def wrapper(*args, **kwargs):",
        "        if _HAS_OTEL:",
        '            with _tracer.start_as_current_span(',
        '                f"{module_name}.{func_name}",',
        '                attributes={"code.function": func_name, "code.namespace": module_name},',
        "            ):",
        "                return await original_func(*args, **kwargs)",
        "        else:",
        "            return await original_func(*args, **kwargs)",
        "    return wrapper",
        "",
        "",
        "def patch_all():",
        f'    """Patch {len(targets)} functions with OTel tracing."""',
        "    patched = 0",
    ]

    for module, units in sorted(module_groups.items()):
        lines.append(f"")
        lines.append(f"    # ── {module} ──")
        lines.append(f"    try:")
        lines.append(f"        import {module}")
        for unit in units:
            wrapper = "_wrap_async_function" if unit.unit_type == "async_function" else "_wrap_function"
            lines.append(
                f'        {module}.{unit.name} = {wrapper}("{module}", "{unit.name}", {module}.{unit.name})'
            )
            lines.append(f"        patched += 1")
        lines.append(f"    except (ImportError, AttributeError) as e:")
        lines.append(f'        log.debug("Skip {module}: %s", e)')

    lines.extend([
        "",
        f'    log.info("Inferra auto-instrumentation: patched %d/{len(targets)} functions", patched)',
        "    return patched",
        "",
        "",
        'if __name__ == "__main__":',
        "    logging.basicConfig(level=logging.INFO)",
        "    patch_all()",
        f'    print("✅ Auto-instrumentation active. Run your app now.")',
        "",
    ])

    script = "\n".join(lines)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        f.write(script)

    log.info("Generated instrumentation script: %s (%d functions)", output_path, len(targets))
    return os.path.abspath(output_path)


def _filepath_to_module(filepath: str) -> str:
    """Convert a file path to a Python importable module name.

    Handles common project layouts:
    - src/app/routes.py  → app.routes (strips src/)
    - ./myapp/views.py   → myapp.views
    - lib/utils.py       → utils (strips lib/)
    """
    path = Path(filepath)
    parts = list(path.parts)

    # Remove leading . or cwd markers
    while parts and parts[0] in (".", "..", ""):
        parts.pop(0)

    # Strip common non-importable prefixes
    non_importable = {"src", "lib", "source", "sources"}
    while parts and parts[0].lower() in non_importable:
        parts.pop(0)

    # Remove .py extension from the last part
    if parts:
        name = parts[-1]
        if name.endswith(".py"):
            parts[-1] = name[:-3]
        elif name == "__init__":
            parts.pop()  # package init → use parent as module

    # Remove __init__ if it's the leaf
    if parts and parts[-1] == "__init__":
        parts.pop()

    return ".".join(parts) if parts else "unknown"
