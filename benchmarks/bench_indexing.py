"""
bench_indexing.py — Indexing Speed Benchmark

Measures how fast the CodeIndexer processes codebases of varying sizes.

Usage:
    python -m benchmarks.bench_indexing [path_to_project]

Default: benchmarks against the Inferra codebase itself.
"""

import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def bench_indexing(project_path: str, runs: int = 3):
    """Benchmark indexing speed for a given project."""
    from inferra.indexer import CodeIndexer

    print(f"\n{'='*60}")
    print(f"  Indexing Benchmark: {os.path.basename(project_path)}")
    print(f"{'='*60}\n")

    times = []
    stats = None

    for i in range(runs):
        indexer = CodeIndexer()
        start = time.perf_counter()
        indexer.index_directory(project_path)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
        stats = indexer.stats()
        print(f"  Run {i+1}/{runs}: {elapsed:.3f}s "
              f"({stats['total_units']} units, {stats['files_indexed']} files)")

    avg = sum(times) / len(times)
    best = min(times)
    worst = max(times)

    print(f"\n  Results:")
    print(f"    Average:  {avg:.3f}s")
    print(f"    Best:     {best:.3f}s")
    print(f"    Worst:    {worst:.3f}s")
    if stats:
        units_per_sec = stats['total_units'] / avg if avg > 0 else 0
        print(f"    Throughput: {units_per_sec:.0f} units/sec")
        print(f"    Details:  {stats['functions']}F, {stats['classes']}C, "
              f"{stats['files_indexed']} files")
    print()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bench_indexing(path)
