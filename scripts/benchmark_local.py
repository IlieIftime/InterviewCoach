"""Benchmark the local interview loop with repeatable conversational scenarios."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests


SCENARIOS = {
    "short": "Tenho experiência em plataformas de ML.",
    "concrete": "Liderei a migração, reduzi a latência em 40 por cento e acompanhei o resultado com métricas.",
    "vague": "Foi um projeto complexo e correu bem.",
    "long": (
        "Num projeto de observabilidade alinhei produto e engenharia, defini uma métrica de sucesso, "
        "dividi o trabalho em fases e validei cada mudança com uma experiência controlada antes de fazer o rollout."
    ),
}


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return round(ordered[index], 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="qwen2.5:1.5b-instruct")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    health = requests.get(f"{base}/health", timeout=10)
    health.raise_for_status()
    rows: list[dict[str, object]] = []

    for scenario, answer in SCENARIOS.items():
        for iteration in range(max(1, args.repeat)):
            started = time.perf_counter()
            session = requests.post(
                f"{base}/start",
                json={
                    "persona": "Entrevistador técnico",
                    "instructions": "Faz uma conversa natural e aprofunda respostas vagas.",
                    "questions": ["Fala do teu percurso profissional.", "Que projeto recordas melhor?"],
                    "model": args.model,
                    "max_followups": 2,
                },
                timeout=20,
            )
            session.raise_for_status()
            session_id = session.json()["session_id"]
            ask_started = time.perf_counter()
            response = requests.post(
                f"{base}/ask",
                json={
                    "session_id": session_id,
                    "text_answer": answer,
                    "capture_mode": "text",
                    "listen_ms": 1800,
                },
                timeout=240,
            )
            response.raise_for_status()
            body = response.json()
            total_ms = round((time.perf_counter() - started) * 1000, 2)
            ask_ms = round((time.perf_counter() - ask_started) * 1000, 2)
            rows.append({
                "scenario": scenario,
                "iteration": iteration + 1,
                "total_ms": total_ms,
                "ask_ms": ask_ms,
                "llm_ms": body.get("llm_ms"),
                "reply_chars": len(str(body.get("reply", ""))),
                "question_kind": body.get("question_kind"),
                "is_followup": body.get("is_followup", False),
                "covered": body.get("covered", []),
                "missing": body.get("missing", []),
            })
            requests.post(f"{base}/stop", json={"session_id": session_id}, timeout=20).raise_for_status()

    summary: dict[str, object] = {
        "health": health.json(),
        "latency_ms": {
            "ask_p50": percentile([float(row["ask_ms"]) for row in rows], 0.50),
            "ask_p95": percentile([float(row["ask_ms"]) for row in rows], 0.95),
            "llm_p50": percentile([float(row["llm_ms"]) for row in rows if row["llm_ms"] is not None], 0.50),
            "llm_p95": percentile([float(row["llm_ms"]) for row in rows if row["llm_ms"] is not None], 0.95),
        },
        "runs": rows,
    }
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
