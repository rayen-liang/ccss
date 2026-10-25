import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api import index
from scripts import setup_supabase


class VercelApiTests(unittest.TestCase):
    def test_password_hash_round_trip(self):
        encoded = index._hash_password("a-long-test-password")
        self.assertTrue(index._verify_password("a-long-test-password", encoded))
        self.assertFalse(index._verify_password("wrong-password", encoded))

    def test_supabase_teacher_password_hash_round_trip(self):
        self.assertEqual(setup_supabase.DEFAULT_TEACHER_PASSWORD, "teacheremma123")
        encoded = setup_supabase.hash_password("teacheremma123")
        self.assertTrue(setup_supabase.verify_password("teacheremma123", encoded))
        self.assertFalse(setup_supabase.verify_password("teacher", encoded))

    def test_question_import_maps_exam_json(self):
        question = index._validate_question(
            {
                "id": "G001",
                "category": "grammar",
                "difficulty": "easy",
                "type": "single_choice",
                "question": "She _____ to school every day.",
                "options": ["go", "goes"],
                "answer": "goes",
                "points": 5,
            }
        )
        self.assertEqual(question["question_type"], "choice")
        self.assertEqual(question["difficulty"], "初級")
        self.assertEqual(question["prompt"], "She _____ to school every day.")

    def test_intermediate_level_maps_to_site_difficulty(self):
        question = index._validate_question(
            {
                "category": "grammar",
                "level": "intermediate",
                "type": "fill_blank",
                "question": "They have _____.",
                "accepted_answers": ["finished", "completed"],
                "points": 10,
            }
        )
        self.assertEqual(question["difficulty"], "中高級")
        self.assertEqual(question["accepted_answers"], ["finished", "completed"])

    def test_invalid_question_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            index._validate_question(
                {
                    "category": "grammar",
                    "difficulty": "專家級",
                    "type": "single_choice",
                    "question": "Choose.",
                    "options": ["A", "B"],
                    "answer": "C",
                    "points": 5,
                }
            )
        self.assertEqual(error.exception.status_code, 400)

    def test_student_question_view_does_not_disclose_answer(self):
        question = {
            "id": 4,
            "bank_id": None,
            "category": "Grammar",
            "scope": "",
            "grade_level": "",
            "difficulty": "中級",
            "question_type": "choice",
            "prompt": "Pick one.",
            "options": ["A", "B"],
            "answer": "A",
            "accepted_answers": [],
            "explanation": "Reason",
            "rubric": "",
            "min_word_count": 0,
            "points": 5,
            "is_published": True,
        }
        view = index._question_view(question)
        self.assertNotIn("answer", view)
        self.assertNotIn("explanation", view)
        self.assertEqual(index._question_view(question, reveal=True)["answer"], "A")

    def test_database_url_missing_is_reported_as_service_unavailable(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as error:
                with index.database():
                    pass
        self.assertEqual(error.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
