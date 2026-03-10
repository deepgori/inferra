"""
Quick test: send traces from 2 different services to Inferra.
Run: inferra serve --port 4318 --project ./test_projects/RealWorldApp
Then: python test_multi_service.py
"""
import json
import urllib.request

OTLP_ENDPOINT = "http://localhost:4318/v1/traces"

payload = {
    "resourceSpans": [
        {
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "api-gateway"}}
                ]
            },
            "scopeSpans": [{
                "spans": [
                    {
                        "name": "POST /api/articles",
                        "traceId": "abcdef1234567890abcdef1234567890",
                        "spanId": "1111111111111111",
                        "parentSpanId": "",
                        "kind": 2,
                        "startTimeUnixNano": "1709900000000000000",
                        "endTimeUnixNano":   "1709900002000000000",
                        "attributes": [
                            {"key": "http.method", "value": {"stringValue": "POST"}},
                            {"key": "http.route", "value": {"stringValue": "/api/articles"}}
                        ],
                        "status": {"code": 1}
                    },
                    {
                        "name": "validate_token",
                        "traceId": "abcdef1234567890abcdef1234567890",
                        "spanId": "2222222222222222",
                        "parentSpanId": "1111111111111111",
                        "kind": 1,
                        "startTimeUnixNano": "1709900000100000000",
                        "endTimeUnixNano":   "1709900000500000000",
                        "attributes": [],
                        "status": {"code": 1}
                    }
                ]
            }]
        },
        {
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "article-service"}}
                ]
            },
            "scopeSpans": [{
                "spans": [
                    {
                        "name": "create_article",
                        "traceId": "abcdef1234567890abcdef1234567890",
                        "spanId": "3333333333333333",
                        "parentSpanId": "1111111111111111",
                        "kind": 1,
                        "startTimeUnixNano": "1709900000600000000",
                        "endTimeUnixNano":   "1709900001800000000",
                        "attributes": [
                            {"key": "code.function", "value": {"stringValue": "create_article"}}
                        ],
                        "status": {"code": 2, "message": "database timeout"}
                    },
                    {
                        "name": "db.query INSERT articles",
                        "traceId": "abcdef1234567890abcdef1234567890",
                        "spanId": "4444444444444444",
                        "parentSpanId": "3333333333333333",
                        "kind": 3,
                        "startTimeUnixNano": "1709900000700000000",
                        "endTimeUnixNano":   "1709900001700000000",
                        "attributes": [
                            {"key": "db.system", "value": {"stringValue": "postgresql"}}
                        ],
                        "status": {"code": 2, "message": "connection timeout after 1000ms"}
                    }
                ]
            }]
        }
    ]
}

data = json.dumps(payload).encode()
req = urllib.request.Request(
    OTLP_ENDPOINT,
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST"
)

try:
    resp = urllib.request.urlopen(req)
    print(f"✅ Sent traces from 2 services: api-gateway + article-service")
    print(f"   Status: {resp.status}")
    print(f"\n   Now hit POST http://localhost:4318/v1/analyze to see '2 Services' in the report!")
except Exception as e:
    print(f"❌ Failed: {e}")
    print("   Make sure 'inferra serve' is running first.")
