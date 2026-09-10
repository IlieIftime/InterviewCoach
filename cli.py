"""Small text-only CLI client for the live interview coach."""
from __future__ import annotations

import json
import os
import urllib.request


BASE_URL = os.getenv("LIVE_COACH_URL", "http://127.0.0.1:8000")


def post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def main() -> None:
    started = post(
        "/start",
        {
            "persona": "Mollie Talent Acquisition interviewer",
            "instructions": "Probe motivation, ownership, and honest technical depth.",
            "questions": [
                "Tell me about a machine learning pipeline you built.",
                "Why do you want to work on an ML platform?",
            ],
        },
    )
    print("Question:", started["current_question"])
    answer = input("Answer: ")
    result = post("/ask", {"session_id": started["session_id"], "text_answer": answer})
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(post("/stop", {"session_id": started["session_id"]}), indent=2))


if __name__ == "__main__":
    main()