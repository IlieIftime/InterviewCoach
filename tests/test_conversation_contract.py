import tempfile
import unittest
import gc
from pathlib import Path

from app import Session, forbidden_question, interview_corpus, next_distinct_planned_question, parse_json, question_similarity, repeated_question
from session_store import SessionStore


class ConversationContractTests(unittest.TestCase):
    def make_session(self, store=None):
        return Session(
            persona="Entrevistador",
            instructions="Aprofunda respostas.",
            avoid="Não inventar.",
            candidate_context="",
            questions=["Fala do teu percurso profissional.", "Que projeto recordas melhor?"],
            questions_to_ask=["Fala do teu percurso profissional.", "Que projeto recordas melhor?"],
            questions_to_avoid=["qual é o teu salário"],
        )

    def test_new_schema_is_normalized(self):
        result = parse_json(
            '{"reply":"Percebi.","question":"Podes dar um exemplo concreto?",'
            '"question_kind":"followup","should_advance":false,"covered":["contexto"]}'
        )
        self.assertEqual(result["reply"], "Percebi.")
        self.assertEqual(result["question_kind"], "followup")
        self.assertFalse(result["should_advance"])
        self.assertEqual(result["followups"], ["Podes dar um exemplo concreto?"])

    def test_legacy_schema_remains_compatible(self):
        result = parse_json(
            '{"feedback":["Boa resposta"],"followups":["Que impacto tiveste?"],'
            '"next_question":"Que impacto tiveste?","score":4,"notes":"Concreta."}'
        )
        self.assertEqual(result["question"], "Que impacto tiveste?")
        self.assertEqual(result["question_kind"], "followup")
        self.assertIn("reply", result)

    def test_truncated_json_recovers_spoken_fields(self):
        result = parse_json('{"reply":"Entendi o que disseste", "question":"Que resultado obtiveste?"')
        self.assertEqual(result["reply"], "Entendi o que disseste")
        self.assertEqual(result["question"], "Que resultado obtiveste?")

    def test_forbidden_question_is_detected(self):
        session = self.make_session()
        self.assertTrue(forbidden_question("Qual é o teu salário atual?", session))
        self.assertFalse(forbidden_question("Que resultado obtiveste no projeto?", session))

    def test_semantic_question_repeat_is_detected(self):
        session = self.make_session()
        session.history.append({"question": "Qual foi o teu projeto técnico mais relevante?", "answer": "Usei cache."})
        self.assertGreaterEqual(question_similarity("Que projeto técnico foi mais relevante para ti?", session.history[0]["question"]), 0.68)
        self.assertTrue(repeated_question("Que projeto técnico foi mais relevante para ti?", session))

    def test_corpus_contains_answer_and_previous_questions(self):
        session = self.make_session()
        session.history.append({"question": "Fala do teu percurso profissional.", "answer": "Trabalhei em ML."})
        corpus = interview_corpus(session, "Reduzi a latência em 40%.")
        self.assertIn("Reduzi a latência", corpus)
        self.assertIn("Trabalhei em ML", corpus)

    def test_next_planned_skips_duplicate_question(self):
        session = self.make_session()
        session.history.append({"question": "Fala do teu percurso profissional.", "answer": "Trabalhei em ML."})
        session.current_index = 0
        self.assertEqual(next_distinct_planned_question(session), "Que projeto recordas melhor?")

    def test_store_migrates_metrics_and_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            session = self.make_session()
            store.save("session-1", session)
            store.record_metric(
                "session-1", "ask", turn_index=1, listen_ms=1200, audio_ms=3000,
                followup_count=1, stt_confidence=0.82, stt_confirmed=True,
            )
            store.save_feedback("session-1", nps_score=9, notes="natural")
            stored = store.get("session-1", Session)
            self.assertEqual(stored.questions[0], session.questions[0])
            self.assertEqual(store.metrics("session-1")[0]["listen_ms"], 1200)
            self.assertEqual(store.feedback("session-1")["nps_score"], 9)
            del store
            gc.collect()


if __name__ == "__main__":
    unittest.main()
