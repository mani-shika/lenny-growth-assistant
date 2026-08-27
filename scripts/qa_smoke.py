#!/usr/bin/env python
"""End-to-end QA against a running instance.

Complements the unit suite, which stubs the model. This drives the real API
with the real model and the real corpus, and asserts the behaviour a reviewer
will actually check: grounding, citation resolution, honest refusal, session
isolation, artifact sanitisation, and structured errors.

    python scripts/qa_smoke.py [--base-url http://localhost:8000]

Exits non-zero if any check fails. Cleans up the sessions it creates.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []
created_sessions: list[str] = []
BASE = "http://localhost:8000"


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    icon = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn "}[status]
    print(f"[{icon}] {name}" + (f"\n           {detail}" if detail else ""), flush=True)


def check(name: str, condition: bool, detail: str = "") -> bool:
    record(PASS if condition else FAIL, name, "" if condition else detail)
    return condition


def call(method: str, path: str, payload: dict | None = None, timeout: float = 300.0):
    """Return (status_code, parsed_body)."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def new_session(title: str | None = None) -> str:
    """Create a session. Passing no title leaves it as "New chat", which is
    what triggers auto-titling from the first message."""
    _, body = call("POST", "/api/sessions", {"title": title} if title else {})
    session_id = body["id"]
    created_sessions.append(session_id)
    return session_id


def ask(session_id: str, message: str, **extra) -> dict:
    status, body = call(
        "POST", f"/api/sessions/{session_id}/messages", {"message": message, **extra}
    )
    if status != 200:
        # Surface it here rather than letting it resurface as a mystifying
        # "nothing was persisted" failure three checks later.
        record(FAIL, f"turn failed: {message[:40]}", f"HTTP {status}: {str(body)[:200]}")
    return body


# ---------------------------------------------------------------- sections


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 58 - len(title)), flush=True)


def qa_health() -> dict:
    section("1. Health and configuration")
    status, health = call("GET", "/api/health", timeout=60)

    if not check("API reachable", status == 200, f"got HTTP {status}: {health}"):
        print("\nAborting: the API is not responding. Is the stack up?")
        sys.exit(1)

    check("status is ok", health["status"] == "ok", f"status={health['status']} checks={health['checks']}")
    check("database connected", health["database"] is True)
    check("corpus indexed", health["corpus"]["indexed"] is True)
    check(
        "60 documents indexed",
        health["corpus"]["documents"] == 60,
        f"got {health['corpus']['documents']}",
    )
    check(
        "all chunks embedded (hybrid search live)",
        health["corpus"]["embedded_chunks"] == health["corpus"]["chunks"],
        f"{health['corpus']['embedded_chunks']}/{health['corpus']['chunks']}",
    )
    check("no outstanding checks", health["checks"] == [], str(health["checks"]))
    check(
        "four providers reported",
        {p["name"] for p in health["providers"]} == {"ollama", "groq", "openai", "anthropic"},
    )
    active = next(p for p in health["providers"] if p["active"])
    check("active provider reachable", active["reachable"] is True, active["detail"])
    print(f"           active: {active['name']} / {active['model']}")

    _, cfg = call("GET", "/api/config", timeout=60)
    check("config endpoint exposes no secrets", not any(
        "key" in json.dumps(cfg).lower().split(k)[0][-12:] and len(str(v)) > 20
        for k, v in [("api_key", "")]
    ) and "sk-" not in json.dumps(cfg))
    return health


def qa_grounded() -> None:
    section("2. Grounded answer and citations")
    session_id = new_session("qa-grounded")

    started = time.perf_counter()
    body = ask(session_id, "How do you know when you have product/market fit?")
    elapsed = time.perf_counter() - started

    assistant = body["assistant_message"]
    check("answer returned", bool(assistant["content"].strip()))
    check("routed to qa", body["route"]["skill"] == "qa", body["route"]["skill"])
    check("grounded", body["grounded"] is True)
    check("retrieval returned passages", body["retrieved_chunks"] > 0)
    check(
        "hybrid retrieval active",
        body["retrieval_strategy"] == "hybrid",
        f"strategy={body['retrieval_strategy']}",
    )
    check("citations present", len(assistant["citations"]) > 0)
    check("provider recorded", assistant["provider"] not in (None, "", "none"))
    check("latency recorded", (assistant["latency_ms"] or 0) > 0)
    print(f"           {assistant['provider']}/{assistant['model']} in {elapsed:.1f}s")

    if assistant["citations"]:
        first = assistant["citations"][0]
        check("citation names its document", bool(first["document_title"]))
        with_ts = [c for c in assistant["citations"] if c.get("timestamp")]
        if with_ts:
            deep = [c for c in with_ts if c.get("source_url") and ("t=" in c["source_url"])]
            check(
                "timestamped citations deep-link to the moment",
                bool(deep),
                "timestamps present but no t= fragment in any URL",
            )
        else:
            record(WARN, "no timestamped citations in this answer", "newsletter sources carry no timestamps")

    if body["citations_matched"]:
        markers = [c["marker"] for c in assistant["citations"] if c.get("marker")]
        check("markers resolve to retrieved passages", all(
            1 <= m <= body["retrieved_chunks"] for m in markers
        ), str(markers))
    else:
        record(WARN, "model emitted no [n] markers", "UI labels these 'sources consulted' - honesty path")

    check("no literal [n] placeholder leaked", "[n]" not in assistant["content"])


def qa_followup() -> None:
    section("3. Session context and isolation")
    first = new_session()  # untitled, so auto-titling from the first message applies
    ask(first, "How do you know when you have product/market fit?")
    body = ask(first, "What about for B2B?")
    check("follow-up answered", bool(body["assistant_message"]["content"].strip()))
    check("follow-up still retrieves", body["retrieved_chunks"] > 0)

    # Read immediately after the write, with no delay. This is what a SPA does
    # and what caught the commit-in-teardown race.
    _, detail = call("GET", f"/api/sessions/{first}", timeout=60)
    check(
        "history readable immediately after write",
        len(detail["messages"]) == 4,
        f"{len(detail['messages'])} messages - read-after-write race?",
    )

    second = new_session("qa-isolation")
    _, other = call("GET", f"/api/sessions/{second}", timeout=60)
    check("new session starts empty", other["messages"] == [])

    check(
        "session titled from first message",
        detail["title"].lower().startswith("how do you know"),
        detail["title"],
    )


def qa_refusal() -> None:
    section("4. Honest refusal (the trust guarantee)")
    session_id = new_session("qa-refusal")

    for question in (
        "What is the treatment protocol for acute pancreatitis?",
        "How do I replace the timing belt on a Honda Civic?",
    ):
        started = time.perf_counter()
        body = ask(session_id, question)
        elapsed = time.perf_counter() - started
        label = question[:42]

        check(f"refused: {label}", body["grounded"] is False, "answered an out-of-corpus question")
        check(f"no passages retrieved: {label}", body["retrieved_chunks"] == 0)
        check(
            f"no model call: {label}",
            body["assistant_message"]["provider"] == "none",
            f"provider={body['assistant_message']['provider']}",
        )
        check(f"refusal is fast (<2s): {label}", elapsed < 2.0, f"{elapsed:.2f}s")
        check(
            f"refusal explains itself: {label}",
            "could not find" in body["assistant_message"]["content"].lower(),
        )

    body = ask(session_id, "What separates a great product manager from a good one?")
    check("in-corpus question still answered", body["grounded"] is True, "gate is over-refusing")


def qa_artifact() -> None:
    section("5. Artifact generation and sanitisation")
    session_id = new_session("qa-artifact")

    body = ask(
        session_id,
        "Make an HTML one-pager on what makes a great product manager",
        skill="artifact",
    )
    check("routed to artifact", body["route"]["skill"] == "artifact")
    artifact = body.get("artifact")
    if not check("artifact produced", artifact is not None):
        return

    check("artifact is HTML as requested", artifact["kind"] == "html", f"kind={artifact['kind']}")
    check(
        "title is not a preamble",
        not artifact["title"].lower().startswith(("here is", "here's", "sure", "below is")),
        artifact["title"],
    )
    content = artifact["content"]
    check("wrapped as a full document", content.lstrip().lower().startswith("<!doctype html"))
    check("carries a locked-down CSP", "default-src 'none'" in content)
    check("no <script> survived", "<script" not in content.lower())
    check("no inline event handlers survived", not any(
        h in content.lower() for h in ("onclick=", "onerror=", "onload=")
    ))
    check("sanitiser report stored", isinstance(artifact["sanitiser_report"], dict))

    status, fetched = call("GET", f"/api/artifacts/{artifact['id']}", timeout=60)
    check("artifact retrievable by id", status == 200 and fetched["content"] == content)

    body = ask(session_id, "Now make a markdown checklist for running user interviews", skill="artifact")
    md = body.get("artifact")
    if md:
        check("markdown artifact produced", md["kind"] == "markdown", f"kind={md['kind']}")


def qa_errors() -> None:
    section("6. Structured errors and validation")
    session_id = new_session("qa-errors")

    status, body = call("GET", "/api/sessions/does-not-exist", timeout=60)
    check("missing session -> 404", status == 404, f"got {status}")
    check("error has a stable code", body.get("error", {}).get("code") == "session_not_found")
    check("error has an operator hint", bool(body.get("error", {}).get("hint")))
    check("error has a request id", bool(body.get("error", {}).get("request_id")))

    status, body = call("GET", "/api/artifacts/nope", timeout=60)
    check("missing artifact -> 404", status == 404, f"got {status}")

    for payload, label in (
        ({"message": "   "}, "whitespace-only message"),
        ({}, "missing message"),
        ({"message": "hi", "skill": "not_a_skill"}, "invalid skill"),
        ({"message": "hi", "provider": "not_a_provider"}, "invalid provider"),
    ):
        status, body = call("POST", f"/api/sessions/{session_id}/messages", payload, timeout=60)
        check(
            f"{label} -> 422 not 500",
            status == 422,
            f"got {status}: {str(body)[:120]}",
        )


def qa_persistence() -> None:
    section("7. Persistence and cleanup")
    session_id = new_session("qa-persistence")
    ask(session_id, "What separates a great product manager from a good one?")

    _, detail = call("GET", f"/api/sessions/{session_id}", timeout=60)
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"]
    check("assistant turn persisted", len(assistant) == 1)
    if assistant:
        check("citations persisted, not just returned", len(assistant[0]["citations"]) > 0)
        check("provenance persisted", bool(assistant[0]["provider"]))
        check("token usage persisted", (assistant[0]["usage"] or {}).get("total_tokens", 0) > 0)

    status, _ = call("DELETE", f"/api/sessions/{session_id}", timeout=60)
    check("delete returns 204", status == 204, f"got {status}")
    created_sessions.remove(session_id)
    status, _ = call("GET", f"/api/sessions/{session_id}", timeout=60)
    check(
        "deleted session is gone immediately",
        status == 404,
        f"got {status} - delete not committed before the response returned",
    )


def cleanup() -> None:
    for session_id in list(created_sessions):
        call("DELETE", f"/api/sessions/{session_id}", timeout=60)
    print(f"\ncleaned up {len(created_sessions)} QA session(s)")


def main() -> int:
    global BASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE)
    parser.add_argument("--skip-cleanup", action="store_true")
    args = parser.parse_args()
    BASE = args.base_url.rstrip("/")

    print(f"QA smoke test against {BASE}")
    started = time.perf_counter()

    qa_health()
    qa_grounded()
    qa_followup()
    qa_refusal()
    qa_artifact()
    qa_errors()
    qa_persistence()

    if not args.skip_cleanup:
        cleanup()

    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    warned = sum(1 for s, _, _ in results if s == WARN)

    print("\n" + "=" * 66)
    print(f"  {passed} passed, {failed} failed, {warned} warnings "
          f"in {time.perf_counter() - started:.0f}s")
    if failed:
        print("\n  Failures:")
        for status, name, detail in results:
            if status == FAIL:
                print(f"    - {name}" + (f": {detail}" if detail else ""))
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
