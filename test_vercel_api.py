import os
import io
import unittest
import zipfile
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

    def test_csv_question_import_matches_streamlit_field_format(self):
        content = (
            "category,scope,grade_level,difficulty,type,prompt,options,answer,"
            "accepted_answers,explanation,rubric,min_word_count,points\n"
            "Grammar,Present simple,Grade 7,初級,choice,Choose the correct form.,"
            "go|goes,goes,,Third-person singular.,,,5\n"
        ).encode()
        payload = index._parse_question_import_file("questions.csv", content)
        validated, _, _ = index._prepare_question_import(payload)
        self.assertEqual(validated[0]["scope"], "Present simple")
        self.assertEqual(validated[0]["grade_level"], "Grade 7")
        self.assertEqual(validated[0]["options"], ["go", "goes"])
        self.assertEqual(validated[0]["answer"], "goes")

    def test_xlsx_question_import_reads_shared_strings_and_inline_values(self):
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr(
                "xl/sharedStrings.xml",
                f'<sst xmlns="{namespace}"><si><t>category</t></si>'
                "<si><t>difficulty</t></si><si><t>type</t></si>"
                "<si><t>prompt</t></si><si><t>options</t></si>"
                "<si><t>answer</t></si><si><t>Grammar</t></si>"
                "<si><t>初級</t></si><si><t>choice</t></si>"
                "<si><t>Choose.</t></si><si><t>A|B</t></si><si><t>A</t></si></sst>",
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                f'<worksheet xmlns="{namespace}"><sheetData>'
                '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
                '<c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c>'
                '<c r="E1" t="s"><v>4</v></c><c r="F1" t="s"><v>5</v></c></row>'
                '<row r="2"><c r="A2" t="s"><v>6</v></c><c r="B2" t="s"><v>7</v></c>'
                '<c r="C2" t="s"><v>8</v></c><c r="D2" t="s"><v>9</v></c>'
                '<c r="E2" t="s"><v>10</v></c><c r="F2" t="s"><v>11</v></c></row>'
                "</sheetData></worksheet>",
            )
        payload = index._parse_question_import_file("questions.xlsx", content.getvalue())
        validated, _, _ = index._prepare_question_import(payload)
        self.assertEqual(validated[0]["prompt"], "Choose.")
        self.assertEqual(validated[0]["options"], ["A", "B"])
        self.assertEqual(validated[0]["answer"], "A")

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
