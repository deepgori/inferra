"""
run_gsquant_instrumented.py — Goldman Sachs gs-quant Instrumented Runner

Wraps gs-quant's timeseries analytics library in a FastAPI application
with OpenTelemetry instrumentation, then sends traffic to generate traces
for Inferra analysis.

Endpoints:
    GET  /api/v1/health
    POST /api/v1/analytics/returns
    POST /api/v1/analytics/statistics
    POST /api/v1/analytics/sharpe
    POST /api/v1/analytics/volatility
    POST /api/v1/analytics/portfolio
    POST /api/v1/analytics/correlation

Architecture:
    FastAPI ──▸ Service Layer ──▸ gs-quant timeseries functions
                     │
                     ├─ DataLoader (generates realistic price data)
                     ├─ AnalyticsEngine (orchestrates computations)
                     └─ PortfolioOptimizer (multi-asset analysis)
"""

import os
import sys
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# ── OTel setup (must be before app imports) ──────────────────────────────────

os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
os.environ.setdefault("OTEL_SERVICE_NAME", "gsquant-analytics-api")

# Add gs-quant to path
GS_QUANT_DIR = os.path.join(
    os.path.dirname(__file__), "test_projects", "gs-quant"
)
sys.path.insert(0, GS_QUANT_DIR)

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Import gs-quant timeseries functions
from gs_quant.timeseries.statistics import (
    mean, median, std, sum_,
    min_, max_, range_, percentile,
    zscores,
)
from gs_quant.timeseries.econometrics import (
    returns, prices, index, change, volatility,
    correlation,
)
from gs_quant.timeseries.analysis import (
    diff, lag, first, last, last_value, count,
)
from gs_quant.timeseries.helper import Window

# OTel tracing — get a tracer for manual spans
from opentelemetry import trace

tracer = trace.get_tracer("gsquant.analytics", "1.0.0")

# ── Source paths for code correlation ────────────────────────────────────────

_STATS_FILE = os.path.join(GS_QUANT_DIR, "gs_quant", "timeseries", "statistics.py")
_ECON_FILE = os.path.join(GS_QUANT_DIR, "gs_quant", "timeseries", "econometrics.py")
_ANALYSIS_FILE = os.path.join(GS_QUANT_DIR, "gs_quant", "timeseries", "analysis.py")


def _span(name, code_file, code_func, code_line=0):
    """Create a span with code.* semantic attributes for source correlation."""
    return tracer.start_as_current_span(
        name,
        attributes={
            "code.filepath": code_file,
            "code.function": code_func,
            "code.lineno": code_line,
            "code.namespace": "gs_quant.timeseries",
        },
    )


# ── Data Layer ───────────────────────────────────────────────────────────────

class DataLoader:
    """Generates realistic financial time series data."""

    TICKERS = {
        "AAPL": {"mu": 0.0008, "sigma": 0.018, "base": 175.0},
        "GOOGL": {"mu": 0.0006, "sigma": 0.020, "base": 140.0},
        "MSFT": {"mu": 0.0007, "sigma": 0.016, "base": 380.0},
        "AMZN": {"mu": 0.0005, "sigma": 0.022, "base": 180.0},
        "NVDA": {"mu": 0.0012, "sigma": 0.028, "base": 800.0},
        "JPM": {"mu": 0.0004, "sigma": 0.015, "base": 195.0},
        "GS": {"mu": 0.0005, "sigma": 0.019, "base": 400.0},
        "BAC": {"mu": 0.0003, "sigma": 0.017, "base": 35.0},
    }

    @staticmethod
    def generate_price_series(
        ticker: str, days: int = 252
    ) -> pd.Series:
        """Generate realistic price series using geometric Brownian motion."""
        with _span("DataLoader.generate_price_series", __file__, "generate_price_series"):
            params = DataLoader.TICKERS.get(
                ticker, {"mu": 0.0005, "sigma": 0.02, "base": 100.0}
            )
            np.random.seed(hash(ticker) % 2**31)
            dates = pd.bdate_range(
                end=datetime.now(), periods=days, freq="B"
            )
            n = len(dates)
            daily_returns = np.random.normal(
                params["mu"], params["sigma"], n
            )
            price_path = params["base"] * np.exp(np.cumsum(daily_returns))
            return pd.Series(price_path, index=dates, name=ticker)

    @staticmethod
    def load_portfolio(
        tickers: List[str], days: int = 252
    ) -> Dict[str, pd.Series]:
        """Load price series for multiple tickers."""
        with _span("DataLoader.load_portfolio", __file__, "load_portfolio"):
            return {
                t: DataLoader.generate_price_series(t, days)
                for t in tickers
            }


# ── Analytics Engine ─────────────────────────────────────────────────────────

class AnalyticsEngine:
    """Orchestrates gs-quant timeseries computations."""

    @staticmethod
    def compute_returns_analysis(series: pd.Series, window: int = 22):
        """Full returns analysis: simple returns, log returns, stats."""
        with _span("econometrics.returns", _ECON_FILE, "returns", 55):
            simple_ret = returns(series, 1)

        with _span("statistics.mean", _STATS_FILE, "mean", 38):
            rolling_mean = mean(simple_ret, Window(window, 0))

        with _span("statistics.std", _STATS_FILE, "std", 92):
            rolling_std = std(simple_ret, Window(window, 0))

        with _span("statistics.min_", _STATS_FILE, "min_", 131):
            rolling_min = min_(simple_ret, Window(window, 0))

        with _span("statistics.max_", _STATS_FILE, "max_", 153):
            rolling_max = max_(simple_ret, Window(window, 0))

        with _span("analysis.count", _ANALYSIS_FILE, "count", 219):
            obs_count = count(simple_ret)

        return {
            "total_return": float(
                (series.iloc[-1] / series.iloc[0] - 1) * 100
            ),
            "mean_daily_return": float(simple_ret.mean() * 100),
            "std_daily_return": float(simple_ret.std() * 100),
            "min_return": float(simple_ret.min() * 100),
            "max_return": float(simple_ret.max() * 100),
            "current_rolling_mean": float(rolling_mean.iloc[-1] * 100)
            if len(rolling_mean) > 0 else None,
            "current_rolling_std": float(rolling_std.iloc[-1] * 100)
            if len(rolling_std) > 0 else None,
            "observations": int(obs_count.iloc[-1])
            if len(obs_count) > 0 else 0,
        }

    @staticmethod
    def compute_statistics(series: pd.Series, window: int = 22):
        """Compute comprehensive statistics on a price series."""
        with _span("econometrics.returns", _ECON_FILE, "returns", 55):
            ret = returns(series, 1)

        with _span("statistics.zscores", _STATS_FILE, "zscores", 280):
            z = zscores(series, Window(window, 0))

        with _span("statistics.mean", _STATS_FILE, "mean", 38):
            m = mean(series)

        with _span("statistics.median", _STATS_FILE, "median", 66):
            med = median(series)

        with _span("statistics.std", _STATS_FILE, "std", 92):
            s = std(ret, Window(window, 0))

        with _span("statistics.range_", _STATS_FILE, "range_", 175):
            r = range_(series, Window(window, 0))

        with _span("analysis.last_value", _ANALYSIS_FILE, "last_value", 196):
            lv = last_value(series)

        with _span("analysis.first", _ANALYSIS_FILE, "first", 172):
            fv = first(series)

        with _span("analysis.count", _ANALYSIS_FILE, "count", 219):
            ct = count(series)

        return {
            "mean": float(m.iloc[-1]),
            "median": float(med.iloc[-1]),
            "std": float(s.iloc[-1] * 100),
            "range": float(r.iloc[-1]),
            "last_value": float(lv),
            "first_value": float(fv.iloc[0]),
            "count": int(ct.iloc[-1]),
            "z_score": float(z.iloc[-1]) if len(z) > 0 else None,
        }

    @staticmethod
    def compute_volatility(series: pd.Series, window: int = 22):
        """Annualized volatility computation."""
        with _span("econometrics.returns", _ECON_FILE, "returns", 55):
            ret = returns(series, 1)

        with _span("statistics.std", _STATS_FILE, "std", 92):
            rolling_vol = std(ret, Window(window, 0))

        # Annualize (252 trading days)
        ann_vol = rolling_vol * np.sqrt(252)

        return {
            "current_volatility_ann": float(ann_vol.iloc[-1] * 100)
            if len(ann_vol) > 0 else None,
            "mean_volatility_ann": float(ann_vol.mean() * 100)
            if len(ann_vol) > 0 else None,
            "max_volatility_ann": float(ann_vol.max() * 100)
            if len(ann_vol) > 0 else None,
            "min_volatility_ann": float(ann_vol.min() * 100)
            if len(ann_vol) > 0 else None,
        }


class PortfolioOptimizer:
    """Multi-asset portfolio analysis using gs-quant."""

    @staticmethod
    def analyze_portfolio(
        portfolio: Dict[str, pd.Series], window: int = 22
    ):
        """Run full portfolio analysis."""
        results = {}
        all_returns = {}

        for ticker, series in portfolio.items():
            with _span("econometrics.returns", _ECON_FILE, "returns", 55):
                ret = returns(series, 1)
            all_returns[ticker] = ret

            with _span("statistics.std", _STATS_FILE, "std", 92):
                vol = std(ret, Window(window, 0))

            results[ticker] = {
                "total_return_pct": float(
                    (series.iloc[-1] / series.iloc[0] - 1) * 100
                ),
                "annualized_vol_pct": float(vol.iloc[-1] * np.sqrt(252) * 100)
                if len(vol) > 0 else None,
                "last_price": float(series.iloc[-1]),
            }

        # Compute pairwise correlations
        corr_matrix = {}
        tickers = list(all_returns.keys())
        for i, t1 in enumerate(tickers):
            for t2 in tickers[i + 1:]:
                try:
                    with _span("econometrics.correlation", _ECON_FILE, "correlation", 310):
                        corr = correlation(
                            all_returns[t1], all_returns[t2],
                            Window(window, 0)
                        )
                    corr_matrix[f"{t1}/{t2}"] = float(corr.iloc[-1]) \
                        if len(corr) > 0 else None
                except Exception:
                    corr_matrix[f"{t1}/{t2}"] = None

        return {
            "assets": results,
            "correlations": corr_matrix,
            "num_assets": len(portfolio),
        }


# ── Pydantic Schemas ─────────────────────────────────────────────────────────

class TickerRequest(BaseModel):
    ticker: str = "AAPL"
    days: int = 252
    window: int = 22

class PortfolioRequest(BaseModel):
    tickers: List[str] = ["AAPL", "GOOGL", "MSFT", "NVDA"]
    days: int = 252
    window: int = 22

class CorrelationRequest(BaseModel):
    ticker1: str = "AAPL"
    ticker2: str = "GOOGL"
    days: int = 252
    window: int = 22


# ── FastAPI Application ──────────────────────────────────────────────────────

app = FastAPI(
    title="gs-quant Analytics API",
    description="Goldman Sachs gs-quant timeseries analytics as a REST API",
    version="1.0.0",
)


@app.get("/api/v1/health")
async def health():
    return {
        "status": "healthy",
        "service": "gsquant-analytics-api",
        "gs_quant_version": "latest",
    }


@app.post("/api/v1/analytics/returns")
async def compute_returns(req: TickerRequest):
    """Compute returns analysis for a given ticker."""
    with _span("AnalyticsEngine.compute_returns_analysis", __file__, "compute_returns_analysis"):
        series = DataLoader.generate_price_series(req.ticker, req.days)
        result = AnalyticsEngine.compute_returns_analysis(series, req.window)
    return {"ticker": req.ticker, "analysis": result}


@app.post("/api/v1/analytics/statistics")
async def compute_statistics_endpoint(req: TickerRequest):
    """Compute comprehensive statistics for a given ticker."""
    with _span("AnalyticsEngine.compute_statistics", __file__, "compute_statistics"):
        series = DataLoader.generate_price_series(req.ticker, req.days)
        result = AnalyticsEngine.compute_statistics(series, req.window)
    return {"ticker": req.ticker, "statistics": result}


@app.post("/api/v1/analytics/sharpe")
async def compute_sharpe(req: TickerRequest):
    """Compute Sharpe ratio for a given ticker."""
    with _span("AnalyticsEngine.compute_sharpe", __file__, "compute_sharpe"):
        series = DataLoader.generate_price_series(req.ticker, req.days)
        with _span("econometrics.returns", _ECON_FILE, "returns", 55):
            ret = returns(series, 1)
        # Manual Sharpe: mean(returns) / std(returns) * sqrt(252)
        daily_mean = float(ret.mean())
        daily_std = float(ret.std())
        annual_sharpe = (daily_mean / daily_std) * np.sqrt(252) if daily_std > 0 else 0
    return {
        "ticker": req.ticker,
        "sharpe_ratio": round(annual_sharpe, 4),
        "annualized_return_pct": round(daily_mean * 252 * 100, 2),
        "annualized_vol_pct": round(daily_std * np.sqrt(252) * 100, 2),
    }


@app.post("/api/v1/analytics/volatility")
async def compute_volatility_endpoint(req: TickerRequest):
    """Compute volatility analysis for a given ticker."""
    with _span("AnalyticsEngine.compute_volatility", __file__, "compute_volatility"):
        series = DataLoader.generate_price_series(req.ticker, req.days)
        result = AnalyticsEngine.compute_volatility(series, req.window)
    return {"ticker": req.ticker, "volatility": result}


@app.post("/api/v1/analytics/portfolio")
async def analyze_portfolio(req: PortfolioRequest):
    """Run full portfolio analysis across multiple tickers."""
    with _span("PortfolioOptimizer.analyze_portfolio", __file__, "analyze_portfolio"):
        portfolio = DataLoader.load_portfolio(req.tickers, req.days)
        result = PortfolioOptimizer.analyze_portfolio(portfolio, req.window)
    return {"portfolio": result}


@app.post("/api/v1/analytics/correlation")
async def compute_correlation(req: CorrelationRequest):
    """Compute rolling correlation between two tickers."""
    with _span("AnalyticsEngine.compute_correlation", __file__, "compute_correlation"):
        s1 = DataLoader.generate_price_series(req.ticker1, req.days)
        s2 = DataLoader.generate_price_series(req.ticker2, req.days)
        with _span("econometrics.returns", _ECON_FILE, "returns", 55):
            r1 = returns(s1, 1)
            r2 = returns(s2, 1)
        with _span("econometrics.correlation", _ECON_FILE, "correlation", 310):
            corr = correlation(r1, r2, Window(req.window, 0))
    return {
        "pair": f"{req.ticker1}/{req.ticker2}",
        "current_correlation": float(corr.iloc[-1]) if len(corr) > 0 else None,
        "mean_correlation": float(corr.mean()) if len(corr) > 0 else None,
        "window": req.window,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    # OTel auto-instrumentation
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider = TracerProvider()
        exporter = OTLPSpanExporter(
            endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] + "/v1/traces"
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor().instrument_app(app)
        print("✅ OpenTelemetry instrumentation enabled")
    except ImportError:
        print("⚠️  OTel not installed — traces won't be exported")

    print("🚀 Starting gs-quant Analytics API on http://0.0.0.0:8001")
    print("   Endpoints:")
    print("   POST /api/v1/analytics/returns")
    print("   POST /api/v1/analytics/statistics")
    print("   POST /api/v1/analytics/sharpe")
    print("   POST /api/v1/analytics/volatility")
    print("   POST /api/v1/analytics/portfolio")
    print("   POST /api/v1/analytics/correlation")
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
