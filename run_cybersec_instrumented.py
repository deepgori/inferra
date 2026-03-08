"""
run_cybersec_instrumented.py — Instrumented Cybersecurity Threat Analyst API

Wraps the agentic_cybersec_threat_analyst backend in a FastAPI server with
OpenTelemetry instrumentation and manual spans for each agent/sub-component.

Since the real pipeline requires Qdrant, Ollama (Foundation-Sec-8B), and
external feeds, this runner simulates realistic execution with real
timing characteristics while emitting OTLP spans that map back to the
actual backend source code.
"""

import json
import os
import random
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# ── OpenTelemetry setup ─────────────────────────────────────────────
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

resource = Resource.create({"service.name": "cybersec-threat-analyst"})
provider = TracerProvider(resource=resource)

otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces")
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("cybersec-threat-analyst")
print("✅ OpenTelemetry instrumentation enabled")

# ── Source file paths (relative to backend/) ────────────────────────
_BASE = "backend"
_AGENTS = f"{_BASE}/agents"
_RAG = f"{_BASE}/rag"
_INGESTION = f"{_BASE}/ingestion"
_API = f"{_BASE}/api"

# ── Manual span helper ──────────────────────────────────────────────
@contextmanager
def _span(name: str, filepath: str, func_name: str, lineno: int):
    """Create a span with code.* attributes for source correlation."""
    with tracer.start_as_current_span(name, attributes={
        "code.filepath": filepath,
        "code.function": func_name,
        "code.lineno": lineno,
        "code.namespace": name.rsplit(".", 1)[0] if "." in name else name,
    }) as span:
        yield span


# ── Simulated Data ──────────────────────────────────────────────────
SAMPLE_CVES = {
    "CVE-2021-44228": {
        "id": "CVE-2021-44228",
        "description": "Apache Log4j2 <=2.14.1 JNDI features do not protect against attacker controlled LDAP and other JNDI related endpoints. An attacker who can control log messages or log message parameters can execute arbitrary code loaded from LDAP servers.",
        "cvss": 10.0, "severity": "CRITICAL",
        "cwes": ["CWE-502", "CWE-400", "CWE-20"],
        "attack_vector": "Network", "published": "2021-12-10",
        "affected": ["Apache Log4j 2.x <= 2.14.1"],
        "kev": True, "otx_pulses": 47, "threatfox_iocs": 312,
    },
    "CVE-2023-44487": {
        "id": "CVE-2023-44487",
        "description": "The HTTP/2 protocol allows a denial of service (server resource consumption) because request cancellation can reset many streams quickly, as exploited in the wild in August through October 2023.",
        "cvss": 7.5, "severity": "HIGH",
        "cwes": ["CWE-400"],
        "attack_vector": "Network", "published": "2023-10-10",
        "affected": ["Multiple HTTP/2 implementations"],
        "kev": True, "otx_pulses": 12, "threatfox_iocs": 0,
    },
    "CVE-2024-3094": {
        "id": "CVE-2024-3094",
        "description": "Malicious code was discovered in the upstream tarballs of xz-utils, starting from version 5.6.0. Through a series of complex obfuscations, the liblzma build process extracts a prebuilt object file from a disguised test file existing in the source code, which is then used to modify specific functions in the liblzma code.",
        "cvss": 10.0, "severity": "CRITICAL",
        "cwes": ["CWE-506"],
        "attack_vector": "Network", "published": "2024-03-29",
        "affected": ["xz-utils 5.6.0, 5.6.1"],
        "kev": True, "otx_pulses": 23, "threatfox_iocs": 89,
    },
    "CVE-2024-21762": {
        "id": "CVE-2024-21762",
        "description": "A out-of-bounds write vulnerability in Fortinet FortiOS allows a remote unauthenticated attacker to execute arbitrary code or command via specially crafted HTTP requests.",
        "cvss": 9.8, "severity": "CRITICAL",
        "cwes": ["CWE-787"],
        "attack_vector": "Network", "published": "2024-02-09",
        "affected": ["FortiOS 7.4.0-7.4.2, 7.2.0-7.2.6, 7.0.0-7.0.13"],
        "kev": True, "otx_pulses": 8, "threatfox_iocs": 45,
    },
    "CVE-2023-23397": {
        "id": "CVE-2023-23397",
        "description": "Microsoft Outlook Elevation of Privilege Vulnerability. An attacker who successfully exploited this vulnerability could access a user's Net-NTLMv2 hash which could be used as a basis of an NTLM Relay attack against another service.",
        "cvss": 9.8, "severity": "CRITICAL",
        "cwes": ["CWE-294"],
        "attack_vector": "Network", "published": "2023-03-14",
        "affected": ["Microsoft Outlook for Windows"],
        "kev": True, "otx_pulses": 15, "threatfox_iocs": 67,
    },
}

ATTACK_TECHNIQUES = {
    "CVE-2021-44228": [
        {"id": "T1190", "name": "Exploit Public-Facing Application", "tactic": "Initial Access", "confidence": 0.95},
        {"id": "T1059", "name": "Command and Scripting Interpreter", "tactic": "Execution", "confidence": 0.88},
        {"id": "T1071", "name": "Application Layer Protocol", "tactic": "Command and Control", "confidence": 0.82},
    ],
    "CVE-2023-44487": [
        {"id": "T1499", "name": "Endpoint Denial of Service", "tactic": "Impact", "confidence": 0.92},
        {"id": "T1498", "name": "Network Denial of Service", "tactic": "Impact", "confidence": 0.85},
    ],
    "CVE-2024-3094": [
        {"id": "T1195.002", "name": "Supply Chain Compromise: Software Supply Chain", "tactic": "Initial Access", "confidence": 0.97},
        {"id": "T1027", "name": "Obfuscated Files or Information", "tactic": "Defense Evasion", "confidence": 0.91},
        {"id": "T1543", "name": "Create or Modify System Process", "tactic": "Persistence", "confidence": 0.78},
    ],
    "CVE-2024-21762": [
        {"id": "T1190", "name": "Exploit Public-Facing Application", "tactic": "Initial Access", "confidence": 0.96},
        {"id": "T1210", "name": "Exploitation of Remote Services", "tactic": "Lateral Movement", "confidence": 0.72},
    ],
    "CVE-2023-23397": [
        {"id": "T1557", "name": "Adversary-in-the-Middle", "tactic": "Credential Access", "confidence": 0.90},
        {"id": "T1187", "name": "Forced Authentication", "tactic": "Credential Access", "confidence": 0.95},
        {"id": "T1078", "name": "Valid Accounts", "tactic": "Privilege Escalation", "confidence": 0.80},
    ],
}


# ── Simulated agent functions ───────────────────────────────────────

def sim_fetch_cve(cve_id: str) -> dict:
    """Simulate NVD API fetch — maps to nvd_fetcher.fetch_cve"""
    time.sleep(random.uniform(0.05, 0.15))
    cve_data = SAMPLE_CVES.get(cve_id, {})
    return {
        "description": cve_data.get("description", f"Unknown CVE: {cve_id}"),
        "cvss_score": cve_data.get("cvss", 0),
        "severity": cve_data.get("severity", "UNKNOWN"),
        "cwes": cve_data.get("cwes", []),
        "published": cve_data.get("published", ""),
        "affected_products": cve_data.get("affected", []),
    }

def sim_check_kev(cve_id: str) -> bool:
    """Simulate CISA KEV check — maps to cisa_kev.is_in_kev"""
    time.sleep(random.uniform(0.01, 0.03))
    return SAMPLE_CVES.get(cve_id, {}).get("kev", False)

def sim_fetch_otx(cve_id: str) -> list:
    """Simulate OTX pulse fetch — maps to otx_fetcher.fetch_otx_pulse_by_cve"""
    time.sleep(random.uniform(0.08, 0.25))
    n_pulses = SAMPLE_CVES.get(cve_id, {}).get("otx_pulses", 0)
    return [{"id": f"otx-{i}", "name": f"Pulse {i}", "iocs": []} for i in range(min(n_pulses, 5))]

def sim_fetch_threatfox(cve_id: str) -> list:
    """Simulate ThreatFox IOC fetch — maps to abusech_fetcher.fetch_threatfox_by_cve"""
    time.sleep(random.uniform(0.05, 0.15))
    n_iocs = SAMPLE_CVES.get(cve_id, {}).get("threatfox_iocs", 0)
    return [{"type": "ip:port", "ioc": f"192.168.{random.randint(1,254)}.{random.randint(1,254)}:{random.randint(1024,65535)}", "malware": "Generic"} for _ in range(min(n_iocs, 10))]

def sim_llm_extract(prompt: str) -> dict:
    """Simulate LLM extraction — maps to langchain invoke in cve_extractor"""
    time.sleep(random.uniform(0.3, 0.8))
    return {"summary": "Simulated extraction", "severity_assessment": "CRITICAL"}

def sim_embed_query(text: str) -> list:
    """Simulate BGE-M3 embedding — maps to embedder.embed_query"""
    time.sleep(random.uniform(0.02, 0.06))
    return [random.random() for _ in range(1024)]

def sim_hybrid_search(query: str, top_k: int = 10) -> list:
    """Simulate Qdrant hybrid search — maps to retriever.hybrid_search"""
    time.sleep(random.uniform(0.05, 0.15))
    return [{"id": f"chunk-{i}", "score": random.uniform(0.7, 0.99), "text": f"ATT&CK technique chunk {i}"} for i in range(top_k)]

def sim_llm_classify(prompt: str) -> list:
    """Simulate LLM ATT&CK classification — maps to attack_classifier agent"""
    time.sleep(random.uniform(0.4, 1.0))
    return []

def sim_llm_generate_playbook(prompt: str) -> str:
    """Simulate LLM playbook generation — maps to playbook_generator agent"""
    time.sleep(random.uniform(0.5, 1.2))
    return "# Incident Response Playbook\n## Simulated playbook content"


# ── FastAPI Application ─────────────────────────────────────────────
app = FastAPI(title="Cybersec Threat Analyst (Instrumented)", version="0.1.0")
FastAPIInstrumentor.instrument_app(app)


@app.get("/")
async def root():
    return {"service": "cybersec-threat-analyst", "status": "instrumented"}


@app.get("/api/health")
async def health():
    """Maps to routes.health_check"""
    with _span("routes.health_check", f"{_API}/routes.py", "health_check", 56):
        return {"status": "healthy", "ollama": "connected", "qdrant": "connected"}


@app.get("/api/cve/{cve_id}")
async def get_cve(cve_id: str):
    """Maps to routes.get_cve → nvd_fetcher.fetch_cve"""
    with _span("routes.get_cve", f"{_API}/routes.py", "get_cve", 91):
        with _span("nvd_fetcher.fetch_cve", f"{_INGESTION}/nvd_fetcher.py", "fetch_cve", 18):
            data = sim_fetch_cve(cve_id)
    return data


@app.post("/api/analyze")
async def analyze(request: Request):
    """Full 3-agent pipeline — maps to routes.analyze → graph.invoke"""
    body = await request.json()
    cve_id = body.get("cve_id", "CVE-2021-44228")

    with _span("routes.analyze", f"{_API}/routes.py", "analyze", 193):
        # Agent 1: CVE Extractor
        with _span("agents.cve_extractor_agent", f"{_AGENTS}/cve_extractor.py", "cve_extractor_agent", 43):
            # Sub-steps: NVD fetch, KEV check, OTX, ThreatFox, LLM extraction
            with _span("nvd_fetcher.fetch_cve", f"{_INGESTION}/nvd_fetcher.py", "fetch_cve", 18):
                nvd_data = sim_fetch_cve(cve_id)

            with _span("cisa_kev.is_in_kev", f"{_INGESTION}/cisa_kev.py", "is_in_kev", 38):
                in_kev = sim_check_kev(cve_id)

            with _span("otx_fetcher.fetch_otx_pulse_by_cve", f"{_INGESTION}/otx_fetcher.py", "fetch_otx_pulse_by_cve", 18):
                otx_pulses = sim_fetch_otx(cve_id)

            with _span("abusech_fetcher.fetch_threatfox_by_cve", f"{_INGESTION}/abusech_fetcher.py", "fetch_threatfox_by_cve", 18):
                threatfox_iocs = sim_fetch_threatfox(cve_id)

            with _span("cve_extractor.llm_invoke", f"{_AGENTS}/cve_extractor.py", "_parse_llm_json", 161):
                extracted = sim_llm_extract(f"Analyze {cve_id}")

        # Agent 2: ATT&CK Classifier (RAG)
        with _span("agents.attack_classifier_agent", f"{_AGENTS}/attack_classifier.py", "attack_classifier_agent", 42):
            with _span("embedder.embed_query", f"{_RAG}/embedder.py", "embed_query", 45):
                embedding = sim_embed_query(nvd_data.get("description", ""))

            with _span("retriever.hybrid_search", f"{_RAG}/retriever.py", "hybrid_search", 28):
                chunks = sim_hybrid_search(cve_id)

            with _span("attack_classifier.llm_classify", f"{_AGENTS}/attack_classifier.py", "_parse_llm_json", 134):
                techniques = sim_llm_classify(f"Classify {cve_id}")
                techniques = ATTACK_TECHNIQUES.get(cve_id, [])

        # Agent 3: Playbook Generator
        with _span("agents.playbook_generator_agent", f"{_AGENTS}/playbook_generator.py", "playbook_generator_agent", 39):
            with _span("playbook_generator.llm_generate", f"{_AGENTS}/playbook_generator.py", "_parse_llm_json", 129):
                playbook = sim_llm_generate_playbook(f"Generate playbook for {cve_id}")

    return {
        "cve_id": cve_id,
        "extracted_info": extracted,
        "nvd": nvd_data,
        "kev": in_kev,
        "otx_pulses": len(otx_pulses),
        "threatfox_iocs": len(threatfox_iocs),
        "techniques": techniques,
        "playbook_preview": playbook[:200],
    }


@app.get("/api/feed/recent")
async def recent_feed(days: int = 7, limit: int = 20):
    """Maps to routes.get_recent_feed"""
    with _span("routes.get_recent_feed", f"{_API}/routes.py", "get_recent_feed", 254):
        with _span("nvd_fetcher.fetch_recent", f"{_INGESTION}/nvd_fetcher.py", "fetch_cve", 18):
            time.sleep(random.uniform(0.1, 0.3))
        with _span("cisa_kev.is_in_kev", f"{_INGESTION}/cisa_kev.py", "is_in_kev", 38):
            time.sleep(random.uniform(0.01, 0.05))
    return {"cves": [{"id": f"CVE-2024-{i}", "severity": random.choice(["LOW","MEDIUM","HIGH","CRITICAL"])} for i in range(limit)]}


@app.get("/api/feed/otx")
async def otx_feed(days: int = 7, limit: int = 20):
    """Maps to routes.get_otx_feed"""
    with _span("routes.get_otx_feed", f"{_API}/routes.py", "get_otx_feed", 276):
        with _span("otx_fetcher.fetch_otx_pulse_by_cve", f"{_INGESTION}/otx_fetcher.py", "fetch_otx_pulse_by_cve", 18):
            pulses = sim_fetch_otx("CVE-2021-44228")
    return {"pulses": pulses}


@app.get("/api/feed/threatfox")
async def threatfox_feed(days: int = 7, limit: int = 50):
    """Maps to routes.get_threatfox_feed"""
    with _span("routes.get_threatfox_feed", f"{_API}/routes.py", "get_threatfox_feed", 285):
        with _span("abusech_fetcher.fetch_threatfox_by_cve", f"{_INGESTION}/abusech_fetcher.py", "fetch_threatfox_by_cve", 18):
            iocs = sim_fetch_threatfox("CVE-2024-3094")
    return {"iocs": iocs}


@app.get("/api/stats")
async def dashboard_stats():
    """Maps to routes.get_dashboard_stats"""
    with _span("routes.get_dashboard_stats", f"{_API}/routes.py", "get_dashboard_stats", 303):
        with _span("qdrant_store.collection_info", f"{_RAG}/qdrant_store.py", "get_collection_info", 25):
            time.sleep(random.uniform(0.01, 0.03))
    return {"qdrant_points": 19233, "collections": 1, "ollama": "connected"}


@app.get("/api/history")
async def analysis_history(limit: int = 50, offset: int = 0):
    """Maps to routes.get_history → db.get_analysis_history"""
    with _span("routes.get_history", f"{_API}/routes.py", "get_history", 381):
        with _span("db.get_analysis_history", f"{_BASE}/db.py", "get_analysis_history", 85):
            time.sleep(random.uniform(0.01, 0.04))
    return {"analyses": [], "total": 0}


# ── Main ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 Starting Cybersec Threat Analyst API on http://0.0.0.0:8002")
    print("   Endpoints:")
    print("   POST /api/analyze          — Full 3-agent pipeline")
    print("   GET  /api/cve/{cve_id}     — CVE lookup")
    print("   GET  /api/feed/recent      — Recent CVEs")
    print("   GET  /api/feed/otx         — OTX pulses")
    print("   GET  /api/feed/threatfox   — ThreatFox IOCs")
    print("   GET  /api/stats            — Dashboard stats")
    print("   GET  /api/health           — Health check")
    print("   GET  /api/history          — Analysis history")
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="warning")
