"""
test_multi_stack.py — Tests for SQL Indexer, Config Indexer, and HTTP Propagator

Validates multi-stack support:
- SQL/dbt model parsing
- YAML/docker-compose/.env config parsing
- HTTP context propagation (inject/extract round-trip)
- Integration: indexing a full-stack project with CodeIndexer
"""

import os
import tempfile
import textwrap
import pytest

from inferra.sql_indexer import SQLIndexer, SQLModel
from inferra.config_indexer import ConfigIndexer, ConfigElement
from inferra.indexer import CodeIndexer
from async_content_tracer.context import (
    ContextManager,
    _context_id,
    _parent_span_id,
    _trace_depth,
)
from async_content_tracer.http_propagator import HTTPContextPropagator


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_dbt_model():
    return textwrap.dedent("""\
        WITH fct_invoices_cte AS (
            SELECT
                InvoiceNo AS invoice_id,
                InvoiceDate AS datetime_id,
                {{ dbt_utils.generate_surrogate_key(['StockCode', 'Description', 'UnitPrice']) }} as product_id,
                {{ dbt_utils.generate_surrogate_key(['CustomerID', 'Country']) }} as customer_id,
                Quantity AS quantity,
                Quantity * UnitPrice AS total
            FROM {{ source('retail', 'raw_invoices') }}
            WHERE Quantity > 0
        )
        SELECT
            invoice_id,
            dt.datetime_id,
            dp.product_id,
            dc.customer_id,
            quantity,
            total
        FROM fct_invoices_cte fi
        INNER JOIN {{ ref('dim_datetime') }} dt ON fi.datetime_id = dt.datetime_id
        INNER JOIN {{ ref('dim_product') }} dp ON fi.product_id = dp.product_id
        INNER JOIN {{ ref('dim_customer') }} dc ON fi.customer_id = dc.customer_id
    """)


@pytest.fixture
def sample_report_sql():
    return textwrap.dedent("""\
        SELECT
          c.country,
          c.iso,
          COUNT(fi.invoice_id) AS total_invoices,
          SUM(fi.total) AS total_revenue
        FROM {{ ref('fct_invoices') }} fi
        JOIN {{ ref('dim_customer') }} c ON fi.customer_id = c.customer_id
        GROUP BY c.country, c.iso
        ORDER BY total_revenue DESC
        LIMIT 10
    """)


@pytest.fixture
def sample_docker_compose():
    return textwrap.dedent("""\
        services:
          postgres:
            image: postgres:15
            ports:
              - 5432:5432
            environment:
              - POSTGRES_USER=admin
              - POSTGRES_PASSWORD=secret123
              - POSTGRES_DB=retail
            restart: always
          metabase:
            image: metabase/metabase:v0.49.12
            ports:
              - 3000:3000
            restart: always
    """)


@pytest.fixture
def sample_dbt_profile():
    return textwrap.dedent("""\
        retail:
          target: dev
          outputs:
            dev:
              type: bigquery
              method: service-account
              project: my-project-123
              dataset: retail
              threads: 1
              timeout_seconds: 300
              location: US
    """)


@pytest.fixture
def sample_env():
    return textwrap.dedent("""\
        # Database configuration
        DB_HOST=localhost
        DB_PORT=5432
        DB_NAME=retail
        DB_USER=admin
        DB_PASSWORD=secret123

        # API keys
        OPENAI_API_KEY=sk-abc123
        SODA_API_KEY=f5c4a4f4
    """)


@pytest.fixture
def multi_stack_project(
    sample_dbt_model, sample_report_sql, sample_docker_compose,
    sample_dbt_profile, sample_env
):
    """Create a temporary multi-stack project directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Python files
        py_dir = os.path.join(tmpdir, "dags")
        os.makedirs(py_dir)
        with open(os.path.join(py_dir, "pipeline.py"), "w") as f:
            f.write(textwrap.dedent("""\
                import logging
                logger = logging.getLogger(__name__)

                def run_dbt():
                    \"\"\"Execute dbt transformations.\"\"\"
                    logger.info("Running dbt models")
                    return True

                def load_to_postgres(data):
                    \"\"\"Load data to PostgreSQL database.\"\"\"
                    logger.info(f"Loading {len(data)} records")
                    return True
            """))

        # SQL files
        sql_dir = os.path.join(tmpdir, "models", "transform")
        os.makedirs(sql_dir)
        with open(os.path.join(sql_dir, "fct_invoices.sql"), "w") as f:
            f.write(sample_dbt_model)

        report_dir = os.path.join(tmpdir, "models", "report")
        os.makedirs(report_dir)
        with open(os.path.join(report_dir, "report_customer.sql"), "w") as f:
            f.write(sample_report_sql)

        # Config files
        with open(os.path.join(tmpdir, "docker-compose.yml"), "w") as f:
            f.write(sample_docker_compose)

        with open(os.path.join(tmpdir, "profiles.yml"), "w") as f:
            f.write(sample_dbt_profile)

        with open(os.path.join(tmpdir, ".env"), "w") as f:
            f.write(sample_env)

        yield tmpdir


# ── SQL Indexer Tests ─────────────────────────────────────────────────────────

class TestSQLModel:
    """Test SQL/dbt model parsing."""

    def test_extracts_dbt_refs(self, sample_dbt_model):
        model = SQLModel("fct_invoices.sql", sample_dbt_model)
        assert set(model.dbt_refs) == {"dim_datetime", "dim_product", "dim_customer"}

    def test_extracts_dbt_sources(self, sample_dbt_model):
        model = SQLModel("fct_invoices.sql", sample_dbt_model)
        assert ("retail", "raw_invoices") in model.dbt_sources

    def test_extracts_dbt_macros(self, sample_dbt_model):
        model = SQLModel("fct_invoices.sql", sample_dbt_model)
        assert "dbt_utils.generate_surrogate_key" in model.dbt_macros

    def test_extracts_ctes(self, sample_dbt_model):
        model = SQLModel("fct_invoices.sql", sample_dbt_model)
        assert "fct_invoices_cte" in model.ctes

    def test_extracts_columns(self, sample_dbt_model):
        model = SQLModel("fct_invoices.sql", sample_dbt_model)
        # Should find columns like invoice_id, quantity, total
        col_names = [c.lower() for c in model.columns]
        assert "invoice_id" in col_names
        assert "quantity" in col_names
        assert "total" in col_names

    def test_extracts_where_filters(self, sample_dbt_model):
        model = SQLModel("fct_invoices.sql", sample_dbt_model)
        assert len(model.filters) > 0
        assert any("Quantity" in f for f in model.filters)

    def test_extracts_aggregations(self, sample_report_sql):
        model = SQLModel("report.sql", sample_report_sql)
        assert "COUNT" in model.aggregations
        assert "SUM" in model.aggregations

    def test_extracts_group_by(self, sample_report_sql):
        model = SQLModel("report.sql", sample_report_sql)
        assert len(model.group_by_cols) > 0

    def test_dependencies_include_refs_and_sources(self, sample_dbt_model):
        model = SQLModel("fct_invoices.sql", sample_dbt_model)
        deps = model.dependencies
        assert "dim_datetime" in deps
        assert "retail.raw_invoices" in deps

    def test_signature_contains_model_name(self, sample_dbt_model):
        model = SQLModel("fct_invoices.sql", sample_dbt_model)
        assert "fct_invoices" in model.signature


class TestSQLIndexer:
    """Test SQL indexer producing CodeUnit objects."""

    def test_index_file_returns_code_unit(self, sample_dbt_model):
        with tempfile.NamedTemporaryFile(suffix=".sql", mode="w", delete=False) as f:
            f.write(sample_dbt_model)
            f.flush()
            try:
                indexer = SQLIndexer()
                units = indexer.index_file(f.name)
                assert len(units) == 1
                assert units[0].unit_type == "sql_model"
                assert units[0].name == os.path.splitext(os.path.basename(f.name))[0]
            finally:
                os.unlink(f.name)

    def test_index_directory(self, multi_stack_project):
        indexer = SQLIndexer()
        units = indexer.index_directory(multi_stack_project)
        assert len(units) == 2  # fct_invoices + report_customer
        names = {u.name for u in units}
        assert "fct_invoices" in names
        assert "report_customer" in names

    def test_tokens_contain_searchable_terms(self, sample_dbt_model):
        with tempfile.NamedTemporaryFile(suffix=".sql", mode="w", delete=False) as f:
            f.write(sample_dbt_model)
            f.flush()
            try:
                indexer = SQLIndexer()
                units = indexer.index_file(f.name)
                tokens = units[0].tokens
                assert "sql" in tokens
                assert "dbt" in tokens
                assert "invoices" in [t.lower() for t in tokens]
            finally:
                os.unlink(f.name)

    def test_empty_sql_file_returns_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".sql", mode="w", delete=False) as f:
            f.write("")
            f.flush()
            try:
                indexer = SQLIndexer()
                units = indexer.index_file(f.name)
                assert len(units) == 0
            finally:
                os.unlink(f.name)


# ── Config Indexer Tests ──────────────────────────────────────────────────────

class TestConfigIndexer:
    """Test YAML, docker-compose, .env, and TOML parsing."""

    def test_docker_compose_extracts_services(self, sample_docker_compose):
        with tempfile.NamedTemporaryFile(
            suffix=".yml", mode="w", delete=False, prefix="docker-compose"
        ) as f:
            f.write(sample_docker_compose)
            f.flush()
            try:
                indexer = ConfigIndexer()
                units = indexer.index_file(f.name)
                names = {u.name for u in units}
                assert "postgres" in names or any("postgres" in u.name for u in units)
            finally:
                os.unlink(f.name)

    def test_docker_compose_captures_ports(self, sample_docker_compose):
        with tempfile.NamedTemporaryFile(
            suffix=".yml", mode="w", delete=False
        ) as f:
            f.write(sample_docker_compose)
            f.flush()
            try:
                indexer = ConfigIndexer()
                units = indexer.index_file(f.name)
                # Should capture port info
                all_tokens = []
                for u in units:
                    all_tokens.extend(u.tokens)
                assert "5432" in " ".join(all_tokens) or "port" in " ".join(all_tokens).lower()
            finally:
                os.unlink(f.name)

    def test_env_file_groups_by_category(self, sample_env):
        with tempfile.NamedTemporaryFile(
            suffix=".env", mode="w", delete=False
        ) as f:
            f.write(sample_env)
            f.flush()
            try:
                indexer = ConfigIndexer()
                units = indexer.index_file(f.name)
                types = {u.name for u in units}
                # Should have connection and credential groups
                assert any("connection" in t for t in types)
                assert any("credential" in t for t in types)
            finally:
                os.unlink(f.name)

    def test_dbt_profile_extracts_connection(self, sample_dbt_profile):
        with tempfile.NamedTemporaryFile(
            suffix=".yml", mode="w", delete=False
        ) as f:
            f.write(sample_dbt_profile)
            f.flush()
            try:
                indexer = ConfigIndexer()
                units = indexer.index_file(f.name)
                # Should find the dbt connection config
                assert len(units) > 0
                all_tokens = []
                for u in units:
                    all_tokens.extend(u.tokens)
                token_str = " ".join(all_tokens).lower()
                assert "bigquery" in token_str or "connection" in token_str
            finally:
                os.unlink(f.name)

    def test_index_directory(self, multi_stack_project):
        indexer = ConfigIndexer()
        units = indexer.index_directory(multi_stack_project)
        # Should find docker-compose.yml, profiles.yml, .env
        assert len(units) >= 3

    def test_config_units_have_correct_type(self, multi_stack_project):
        indexer = ConfigIndexer()
        units = indexer.index_directory(multi_stack_project)
        for u in units:
            assert u.unit_type == "config"

    def test_empty_file_returns_empty(self):
        with tempfile.NamedTemporaryFile(
            suffix=".yml", mode="w", delete=False
        ) as f:
            f.write("")
            f.flush()
            try:
                indexer = ConfigIndexer()
                units = indexer.index_file(f.name)
                assert len(units) == 0
            finally:
                os.unlink(f.name)


# ── HTTP Context Propagator Tests ─────────────────────────────────────────────

class TestHTTPContextPropagator:
    """Test cross-service trace context propagation."""

    def test_inject_context_into_headers(self):
        ctx = ContextManager()
        context = ctx.new_context()
        propagator = HTTPContextPropagator(ctx)

        headers = {}
        propagator.inject(headers)

        assert "X-Trace-Context-Id" in headers
        assert headers["X-Trace-Context-Id"] == context.context_id
        assert "X-Trace-Depth" in headers

    def test_extract_restores_context(self):
        ctx = ContextManager()
        original = ctx.new_context()
        propagator = HTTPContextPropagator(ctx)

        # Inject into headers
        headers = {}
        propagator.inject(headers)

        # Clear context
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)

        # Extract should restore
        restored_id = propagator.extract(headers)
        assert restored_id == original.context_id
        assert _context_id.get() == original.context_id

    def test_inject_extract_round_trip(self):
        ctx = ContextManager()
        original = ctx.new_context()
        _trace_depth.set(3)
        propagator = HTTPContextPropagator(ctx)

        # Inject
        headers = propagator.get_context_headers()
        assert len(headers) >= 3

        # Reset
        _context_id.set(None)
        _trace_depth.set(0)

        # Extract
        propagator.extract(headers)
        assert _context_id.get() == original.context_id
        assert _trace_depth.get() == 3

    def test_extract_with_no_context_returns_none(self):
        ctx = ContextManager()
        propagator = HTTPContextPropagator(ctx)
        result = propagator.extract({})
        assert result is None

    def test_w3c_traceparent_emitted(self):
        ctx = ContextManager()
        ctx.new_context()
        propagator = HTTPContextPropagator(ctx)

        headers = {}
        propagator.inject(headers)
        assert "traceparent" in headers
        assert headers["traceparent"].startswith("00-")

    def test_case_insensitive_header_lookup(self):
        ctx = ContextManager()
        original = ctx.new_context()
        propagator = HTTPContextPropagator(ctx)

        headers = {"x-trace-context-id": original.context_id, "x-trace-depth": "2"}
        _context_id.set(None)
        restored = propagator.extract(headers)
        assert restored == original.context_id

    def test_middleware_factory(self):
        ctx = ContextManager()
        propagator = HTTPContextPropagator(ctx)
        middleware = propagator.create_middleware()

        assert "extract_from_request" in middleware
        assert "inject_into_response" in middleware
        assert callable(middleware["extract_from_request"])
        assert callable(middleware["inject_into_response"])

    def test_middleware_creates_new_context_if_none(self):
        ctx = ContextManager()
        _context_id.set(None)
        propagator = HTTPContextPropagator(ctx)
        middleware = propagator.create_middleware()

        context_id = middleware["extract_from_request"]({})
        assert context_id is not None
        assert len(context_id) > 0


# ── Integration Tests ─────────────────────────────────────────────────────────

class TestMultiStackIntegration:
    """Test that CodeIndexer picks up SQL and config files alongside Python."""

    def test_indexes_all_file_types(self, multi_stack_project):
        indexer = CodeIndexer()
        indexer.index_directory(multi_stack_project)
        stats = indexer.stats()

        assert stats["functions"] > 0, "Should index Python functions"
        assert stats["sql_models"] > 0, "Should index SQL models"
        assert stats["config_entries"] > 0, "Should index config entries"

    def test_sql_models_appear_in_search(self, multi_stack_project):
        indexer = CodeIndexer()
        indexer.index_directory(multi_stack_project)

        results = indexer.search("invoice customer revenue", top_k=5)
        assert len(results) > 0
        # Should find SQL models
        unit_types = {r.code_unit.unit_type for r in results}
        assert "sql_model" in unit_types

    def test_config_appears_in_search(self, multi_stack_project):
        indexer = CodeIndexer()
        indexer.index_directory(multi_stack_project)

        results = indexer.search("postgres database connection port", top_k=5)
        assert len(results) > 0

    def test_cross_stack_search_finds_all(self, multi_stack_project):
        indexer = CodeIndexer()
        indexer.index_directory(multi_stack_project)

        # Search for "postgres" — should find config AND Python code
        results = indexer.search("postgres loading data", top_k=10)
        assert len(results) > 0

    def test_total_units_includes_all_types(self, multi_stack_project):
        indexer = CodeIndexer()
        indexer.index_directory(multi_stack_project)

        total = indexer.stats()["total_units"]
        py = indexer.stats()["functions"] + indexer.stats()["classes"]
        sql = indexer.stats()["sql_models"]
        cfg = indexer.stats()["config_entries"]

        assert total >= py + sql + cfg

    def test_real_dbt_project(self):
        """Test on the actual secure-retail-analytics-pipeline repo if available."""
        repo_path = "/tmp/secure-retail-analytics-pipeline"
        if not os.path.isdir(repo_path):
            pytest.skip("Repo not cloned — run: git clone https://github.com/deepgori/secure-retail-analytics-pipeline /tmp/secure-retail-analytics-pipeline")

        indexer = CodeIndexer()
        indexer.index_directory(
            repo_path,
            exclude_patterns=["__pycache__", ".git", "venv", "node_modules", ".astro"]
        )

        stats = indexer.stats()
        assert stats["sql_models"] >= 7, f"Expected ≥7 SQL models, got {stats['sql_models']}"
        assert stats["config_entries"] >= 3, f"Expected ≥3 config entries, got {stats['config_entries']}"
        assert stats["functions"] > 0, "Should index Python functions too"
