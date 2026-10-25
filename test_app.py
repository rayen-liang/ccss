import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import ccss_db as db


class CcssDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp_dir.name) / "test.db"
        db.initialize_database()
        self.student_id = db.register_student(
            "alice", "correct-horse", "alice@example.com"
        )
        self.teacher_id = db.authenticate("teacher", "teacher")["id"]
        self.other_student_id = db.register_student(
            "bob", "another-password", "bob@example.com"
        )

    def tearDown(self):
        db.DB_PATH = self.previous_db_path
        self.temp_dir.cleanup()

    def question(self, **overrides):
        question = {
            "category": "Grammar",
            "difficulty": "初級",
            "type": "choice",
            "prompt": "Choose the correct verb.",
            "options": ["go", "goes"],
            "answer": "goes",
            "explanation": "She takes the third-person singular form.",
            "points": 5,
        }
        question.update(overrides)
        return question

    def add_question(self, question):
        return db.add_question(self.teacher_id, question)

    def test_teacher_bootstrap_requires_password_change(self):
        teacher = db.authenticate("teacher", "teacher")
        self.assertIsNotNone(teacher)
        self.assertEqual(teacher["role"], "teacher")
        self.assertEqual(teacher["must_change_password"], 1)
        self.assertIsNone(db.authenticate("teacher", "wrong"))

    def test_teacher_can_create_class_and_assign_selected_students(self):
        class_id = db.create_class(self.teacher_id, "Grade 7A")
        db.set_class_students(
            self.teacher_id, class_id, [self.student_id, self.other_student_id]
        )
        self.assertEqual(
            db.class_student_ids(self.teacher_id, class_id),
            [self.student_id, self.other_student_id],
        )
        class_record = db.list_classes(self.teacher_id)[0]
        self.assertEqual(class_record["name"], "Grade 7A")
        self.assertEqual(class_record["student_count"], 2)
        overview = db.student_overview(self.teacher_id)
        self.assertTrue(all(student["class_name"] == "Grade 7A" for student in overview))
        with self.assertRaisesRegex(ValueError, "同名班級"):
            db.create_class(self.teacher_id, "grade 7a")
        with self.assertRaisesRegex(ValueError, "僅限啟用中的教師"):
            db.create_class(self.student_id, "Unauthorized")

    def test_student_accuracy_and_rank_depend_on_score_only(self):
        self.add_question(self.question())
        first_question_id = db.list_questions()[0]["id"]
        self.add_question(self.question(prompt="Second question."))
        second_question_id = db.list_questions()[1]["id"]
        db.submit_answer(self.student_id, first_question_id, "goes")
        db.submit_answer(self.other_student_id, first_question_id, "goes")
        db.submit_answer(self.other_student_id, second_question_id, "go")

        rankings = db.leaderboard()
        self.assertEqual(rankings[0]["score"], rankings[1]["score"])
        self.assertEqual(rankings[0]["rank"], 1)
        self.assertEqual(rankings[1]["rank"], 1)
        self.assertEqual(db.student_accuracy(self.student_id), 100)
        self.assertEqual(db.student_accuracy(self.other_student_id), 50)
        overview = {
            row["id"]: row for row in db.student_overview(self.teacher_id)
        }
        self.assertEqual(overview[self.student_id]["rank"], 1)
        self.assertEqual(overview[self.student_id]["accuracy"], 100)

    def test_student_wheel_spends_points_once_and_updates_leaderboard(self):
        for number in range(3):
            prompt = f"Fifty-point question {number}."
            self.add_question(self.question(prompt=prompt, points=50))
            question = next(
                item for item in db.list_questions() if item["prompt"] == prompt
            )
            db.submit_answer(self.student_id, question["id"], "goes")

        state = db.student_wheel_state(self.student_id)
        self.assertEqual(state["score"], 150)
        self.assertTrue(state["can_spin"])
        self.assertFalse(state["special_ready"])

        request_id = "wheel-test-request-standard"
        result = db.spin_student_wheel(self.student_id, request_id)
        self.assertEqual(result["wheel_type"], "standard")
        self.assertEqual(result["points_spent"], 150)
        self.assertIn(result["points_awarded"], {10, 20, 30, 40, 50})
        self.assertEqual(result["score"], result["points_awarded"])

        retry_result = db.spin_student_wheel(self.student_id, request_id)
        self.assertEqual(retry_result, result)
        self.assertEqual(db.student_progress(self.student_id)["total_score"], result["score"])
        self.assertEqual(db.leaderboard()[0]["score"], result["score"])
        self.assertEqual(
            next(
                row["score"]
                for row in db.student_overview(self.teacher_id)
                if row["id"] == self.student_id
            ),
            result["score"],
        )
        with self.assertRaisesRegex(ValueError, "積分不足"):
            db.spin_student_wheel(self.student_id, "wheel-test-request-insufficient")
        with self.assertRaisesRegex(ValueError, "只有啟用中的學生"):
            db.spin_student_wheel(self.teacher_id, "wheel-test-request-teacher")

    def test_student_wheel_unlocks_special_reward_at_each_thousand_points(self):
        for number in range(20):
            prompt = f"Milestone question {number}."
            self.add_question(self.question(prompt=prompt, points=50))
            question = next(
                item for item in db.list_questions() if item["prompt"] == prompt
            )
            db.submit_answer(self.student_id, question["id"], "goes")

        state = db.student_wheel_state(self.student_id)
        self.assertEqual(state["earned_score"], 1000)
        self.assertTrue(state["special_ready"])
        special_result = db.spin_student_wheel(
            self.student_id, "wheel-test-request-milestone"
        )
        self.assertEqual(special_result["wheel_type"], "milestone")
        self.assertEqual(special_result["score_milestone"], 1000)
        self.assertIn(special_result["points_awarded"], {30, 40, 50, 60, 70})
        self.assertFalse(db.student_wheel_state(self.student_id)["special_ready"])

        with db.database() as conn:
            conn.execute(
                """INSERT INTO attempts
                   (student_id, question_id, response, earned_points)
                   SELECT ?, id, 'goes', points FROM questions
                   WHERE category = 'Grammar' AND prompt LIKE 'Milestone question %'""",
                (self.other_student_id,),
            )
        other_state = db.student_wheel_state(self.other_student_id)
        self.assertTrue(other_state["special_ready"])
        other_special = db.spin_student_wheel(
            self.other_student_id, "wheel-test-request-second-user"
        )
        self.assertEqual(other_special["score_milestone"], 1000)

        for number in range(20, 40):
            prompt = f"Milestone question {number}."
            self.add_question(self.question(prompt=prompt, points=50))
            question = next(
                item for item in db.list_questions() if item["prompt"] == prompt
            )
            db.submit_answer(self.student_id, question["id"], "goes")
        state = db.student_wheel_state(self.student_id)
        self.assertEqual(state["earned_score"], 2000)
        self.assertTrue(state["special_ready"])
        second_special = db.spin_student_wheel(
            self.student_id, "wheel-test-request-milestone-2000"
        )
        self.assertEqual(second_special["score_milestone"], 2000)

    def test_daily_login_rewards_are_idempotent_and_follow_streak_milestones(self):
        start = date(2026, 1, 1)
        rewards = [
            db.claim_daily_login_reward(
                self.student_id, (start + timedelta(days=offset)).isoformat()
            )
            for offset in range(30)
        ]
        self.assertEqual([reward["points_awarded"] for reward in rewards[:5]], [15, 15, 15, 15, 30])
        self.assertEqual(rewards[9]["points_awarded"], 50)
        self.assertEqual(rewards[19]["streak_day"], 20)
        self.assertTrue(rewards[19]["free_wheel_awarded"])
        self.assertEqual(rewards[29]["streak_day"], 30)
        self.assertTrue(rewards[29]["free_wheel_awarded"])
        self.assertIsNone(
            db.claim_daily_login_reward(
                self.student_id, (start + timedelta(days=29)).isoformat()
            )
        )

        state = db.student_wheel_state(self.student_id)
        self.assertEqual(state["free_spins"], 2)
        self.assertTrue(state["can_spin"])
        result = db.spin_student_wheel(
            self.student_id, "wheel-test-request-streak-free"
        )
        self.assertEqual(result["wheel_type"], "streak")
        self.assertEqual(result["streak_day"], 20)
        self.assertEqual(result["points_spent"], 0)
        self.assertIn(result["points_awarded"], {10, 20, 30, 40, 50})
        self.assertEqual(
            db.student_progress(self.student_id)["total_score"],
            sum(reward["points_awarded"] for reward in rewards)
            + result["points_awarded"],
        )
        self.assertEqual(db.student_wheel_state(self.student_id)["free_spins"], 1)
        second_free_spin = db.spin_student_wheel(
            self.student_id, "wheel-test-request-streak-free-2"
        )
        self.assertEqual(second_free_spin["wheel_type"], "streak")
        self.assertEqual(second_free_spin["streak_day"], 30)
        self.assertEqual(db.student_wheel_state(self.student_id)["free_spins"], 0)
        self.assertEqual(
            db.leaderboard()[0]["score"],
            db.student_progress(self.student_id)["total_score"],
        )
        self.assertEqual(
            next(
                row["score"]
                for row in db.student_overview(self.teacher_id)
                if row["id"] == self.student_id
            ),
            db.student_progress(self.student_id)["total_score"],
        )

    def test_successful_login_claims_daily_reward_once_but_teacher_does_not(self):
        first_login = db.login("alice", "correct-horse")
        self.assertEqual(first_login["daily_reward"]["points_awarded"], 15)
        self.assertEqual(first_login["daily_reward"]["streak_day"], 1)
        second_login = db.login("alice", "correct-horse")
        self.assertIsNone(second_login["daily_reward"])
        teacher_login = db.login("teacher", "teacher")
        self.assertIsNone(teacher_login.get("daily_reward"))
        self.assertIsNone(db.login("alice", "incorrect"))

    def test_login_streak_resets_after_a_missed_day(self):
        start = date(2026, 2, 1)
        for offset in range(4):
            db.claim_daily_login_reward(
                self.student_id, (start + timedelta(days=offset)).isoformat()
            )
        reset_reward = db.claim_daily_login_reward(
            self.student_id, (start + timedelta(days=6)).isoformat()
        )
        self.assertEqual(reset_reward["streak_day"], 1)
        self.assertEqual(reset_reward["points_awarded"], 15)

    def test_essay_drafts_feedback_comments_revision_and_version_history(self):
        self.add_question(
            self.question(
                type="essay",
                prompt="Describe a meaningful day.",
                options=[],
                answer="",
                points=20,
                min_word_count=5,
                rubric="Content 10 points; grammar 10 points.",
            )
        )
        essay = db.list_questions()[0]
        self.assertEqual(essay["rubric"], "Content 10 points; grammar 10 points.")
        self.assertEqual(essay["min_word_count"], 5)
        with self.assertRaisesRegex(ValueError, "至少需要 5 個英文單字"):
            db.submit_essay(self.student_id, essay["id"], "Short essay.")

        first_version = "Yesterday I visited the museum with my good friend."
        db.save_essay_draft(self.student_id, essay["id"], first_version)
        overview = db.list_student_essays(self.student_id)[0]
        self.assertEqual(overview["draft_content"], first_version)
        submission = db.submit_essay(self.student_id, essay["id"], first_version)
        self.assertEqual(submission["version_number"], 1)
        with self.assertRaisesRegex(ValueError, "等待老師退回"):
            db.submit_essay(self.student_id, essay["id"], first_version)

        attempt = db.list_essay_attempts(self.teacher_id)[0]
        db.add_essay_comment(
            self.teacher_id,
            attempt["attempt_id"],
            1,
            "Yesterday I visited the museum",
            "Consider adding a descriptive adjective.",
        )
        self.assertEqual(
            db.essay_comments(
                attempt["attempt_id"], 1, self.student_id
            )[0]["comment"],
            "Consider adding a descriptive adjective.",
        )
        db.request_essay_revision(
            self.teacher_id,
            attempt["attempt_id"],
            "Please include more detail about what you learned.",
        )
        self.assertEqual(
            db.list_student_essays(self.student_id)[0]["essay_status"],
            "revision_requested",
        )

        revised_content = "Yesterday I visited the beautiful museum with my good friend."
        db.save_essay_draft(self.student_id, essay["id"], revised_content)
        second_version = db.submit_essay(
            self.student_id, essay["id"], revised_content
        )
        self.assertEqual(second_version["version_number"], 2)
        db.grade_essay(
            self.teacher_id,
            attempt["attempt_id"],
            18,
            "Good revision with clearer detail.",
        )
        versions = db.student_essay_versions(
            self.student_id, attempt["attempt_id"]
        )
        self.assertEqual([version["version_number"] for version in versions], [2, 1])
        self.assertEqual(versions[0]["status"], "graded")
        self.assertEqual(versions[0]["score"], 18)
        self.assertEqual(versions[1]["status"], "revision_requested")

    def test_student_registration_and_account_updates(self):
        self.assertIsNone(db.authenticate("alice", "wrong"))
        self.assertEqual(db.authenticate("alice", "correct-horse")["id"], self.student_id)
        with self.assertRaisesRegex(ValueError, "已被使用"):
            db.register_student("alice", "different-password", "new@example.com")
        with self.assertRaisesRegex(ValueError, "已被使用"):
            db.register_student("alice2", "different-password", "alice@example.com")
        db.change_display_name(self.student_id, "Alice Learner")
        db.change_password(self.student_id, "a-new-password")
        self.assertIsNone(db.authenticate("alice", "correct-horse"))
        updated_user = db.authenticate("alice", "a-new-password")
        self.assertEqual(updated_user["username"], "alice")
        self.assertEqual(updated_user["display_name"], "Alice Learner")
        with self.assertRaisesRegex(ValueError, "至少"):
            db.change_password(self.student_id, "short")

    def test_question_bank_import_is_validated_before_writing(self):
        payload = {"questions": [self.question(), self.question(prompt="Another question")]}
        self.assertEqual(db.import_questions(self.teacher_id, payload), 2)
        self.assertEqual(len(db.list_questions()), 2)
        with self.assertRaisesRegex(ValueError, "第 2 題格式錯誤"):
            db.import_questions(
                self.teacher_id,
                {"questions": [self.question(), self.question(difficulty="expert")]}
            )
        self.assertEqual(len(db.list_questions()), 2)
        self.assertEqual(json.loads(json.dumps(db.list_questions()[0]["options"])), ["go", "goes"])

    def test_normalizes_and_imports_exam_json_envelope(self):
        exam = {
            "title": "English Grammar Test 01",
            "category": "grammar",
            "level": "intermediate",
            "questions": [
                {
                    "id": "G001",
                    "type": "single_choice",
                    "difficulty": "easy",
                    "points": 5,
                    "question": "She _____ to school every day.",
                    "options": ["go", "goes", "going", "gone"],
                    "answer": "goes",
                    "explanation": "Use third-person singular.",
                },
                {
                    "id": "G002",
                    "type": "fill_blank",
                    "difficulty": "medium",
                    "points": 10,
                    "question": "They have _____ their homework.",
                    "accepted_answers": ["finished", "completed"],
                    "explanation": "Use the past participle after have.",
                },
            ],
        }
        normalized = db.normalize_question_import(exam)
        self.assertEqual(normalized["title"], "English Grammar Test 01")
        self.assertEqual(normalized["category"], "grammar")
        self.assertEqual(normalized["level"], "intermediate")
        self.assertEqual(
            [(item["type"], item["difficulty"]) for item in normalized["questions"]],
            [("choice", "初級"), ("fill_blank", "中級")],
        )
        self.assertEqual(
            normalized["questions"][0]["prompt"],
            "She _____ to school every day.",
        )
        self.assertEqual(
            normalized["questions"][1]["accepted_answers"],
            ["finished", "completed"],
        )
        bank_id = db.create_question_bank(
            self.teacher_id, normalized["title"]
        )
        self.assertEqual(db.import_questions(self.teacher_id, normalized, bank_id=bank_id), 2)
        with self.assertRaisesRegex(ValueError, "已經匯入過"):
            db.import_questions(self.teacher_id, normalized, bank_id=bank_id)
        imported = db.list_questions(bank_id=bank_id)
        self.assertEqual(len(imported), 2)
        fill_blank = next(
            question
            for question in imported
            if question["question_type"] == "fill_blank"
        )
        self.assertEqual(fill_blank["answer"], "finished")

    def test_choice_scoring_and_leaderboard_use_best_attempt_per_question(self):
        self.add_question(self.question())
        question_id = db.list_questions()[0]["id"]
        self.assertEqual(
            db.submit_answer(self.student_id, question_id, "go")["earned_points"], 0
        )
        self.assertEqual(
            db.submit_answer(self.student_id, question_id, "GOES")["earned_points"], 5
        )
        self.assertEqual(
            db.submit_answer(self.student_id, question_id, "goes")["earned_points"], 5
        )
        self.assertEqual(db.leaderboard()[0]["score"], 5)

    def test_student_progress_counts_attempts_and_best_scores_without_double_counting(self):
        self.add_question(self.question())
        choice_id = db.list_questions()[0]["id"]
        db.submit_answer(self.student_id, choice_id, "go")
        db.submit_answer(self.student_id, choice_id, "goes")
        self.add_question(
            self.question(
                type="essay",
                prompt="Describe your favorite place.",
                options=[],
                answer="",
                points=10,
            )
        )
        essay_id = db.list_questions()[-1]["id"]
        db.submit_answer(self.student_id, essay_id, "My favorite place is the beach.")

        progress = db.student_progress(self.student_id)
        self.assertEqual(progress["attempt_count"], 3)
        self.assertEqual(progress["questions_attempted"], 2)
        self.assertEqual(progress["total_score"], 5)
        self.assertEqual(progress["essays_pending"], 1)
        self.assertEqual(len(db.student_attempts(self.student_id)), 3)

    def test_assignments_show_attempted_status(self):
        self.add_question(self.question())
        question_id = db.list_questions()[0]["id"]
        with self.assertRaisesRegex(ValueError, "教師"):
            db.assign_questions(self.student_id, self.other_student_id, [question_id])
        db.assign_questions(
            self.teacher_id, self.student_id, [question_id, question_id], "2030-01-01"
        )
        assignments = db.list_assignments(self.student_id)
        self.assertEqual(len(assignments), 1)
        self.assertFalse(assignments[0]["attempted"])
        self.assertEqual(assignments[0]["due_at"], "2030-01-01")
        db.submit_answer(self.student_id, question_id, "goes")
        self.assertTrue(db.list_assignments(self.student_id)[0]["attempted"])

    def test_fill_blank_questions_are_auto_scored_case_insensitively(self):
        self.add_question(
            self.question(
                type="fill_blank",
                prompt="She ____ to school every day.",
                options=[],
                answer="goes",
                accepted_answers=["goes", "go"],
                points=4,
            )
        )
        question = db.list_questions()[0]
        quiz_id = db.begin_quiz(self.student_id, [question["id"]])
        result = db.submit_quiz(
            quiz_id, self.student_id, {str(question["id"]): "GOES"}
        )
        self.assertEqual(result["score"], 4)
        self.assertEqual(result["correct_count"], 1)
        self.assertEqual(db.quiz_results(quiz_id, self.student_id)[0]["answer"], "goes")

    def test_assignment_retake_score_is_not_multiplied_and_answers_can_be_hidden(self):
        self.add_question(self.question())
        question_id = db.list_questions()[0]["id"]
        assignment_id = db.create_assignment(
            self.teacher_id,
            "Grammar practice",
            "",
            [question_id],
            student_ids=[self.student_id],
            allow_retake=True,
            show_answers=False,
        )
        for response in ("goes", "goes"):
            quiz_id = db.begin_filtered_quiz(
                self.student_id,
                "Grammar",
                None,
                1,
                assigned_only=True,
                assignment_id=assignment_id,
            )
            db.submit_quiz(quiz_id, self.student_id, {str(question_id): response})
        submission = db.assignment_submissions(
            self.teacher_id, assignment_id
        )[0]
        self.assertEqual(submission["score"], 5)
        self.assertEqual(submission["max_score"], 5)
        self.assertEqual(submission["answered_count"], 1)
        statistics = db.assignment_statistics(self.teacher_id, assignment_id)
        self.assertEqual(statistics["submission_count"], 1)
        self.assertEqual(statistics["average_score"], 5)
        self.assertEqual(statistics["questions"][0]["response_count"], 1)
        self.assertEqual(statistics["questions"][0]["correct_rate"], 1)
        exported_scores = db.export_assignment_scores(
            self.teacher_id, assignment_id
        )
        self.assertIn("student_id,username,display_name", exported_scores)
        self.assertIn("alice,alice,", exported_scores)
        self.assertEqual(db.quiz_results(quiz_id, self.student_id)[0]["answer"], "")
        self.assertEqual(
            db.quiz_results(quiz_id, self.student_id)[0]["explanation"], ""
        )
        own_result = db.quiz_results(quiz_id, self.student_id)[0]
        self.assertEqual(own_result["response"], "goes")
        self.assertEqual(own_result["options"], ["go", "goes"])
        self.assertEqual(db.quiz_results(quiz_id, self.other_student_id), [])
        history_item = db.student_attempts(self.student_id)[0]
        self.assertEqual(history_item["answer"], "")
        self.assertEqual(history_item["explanation"], "")

    def test_assignment_schedule_must_be_valid(self):
        self.add_question(self.question())
        question_id = db.list_questions()[0]["id"]
        with self.assertRaisesRegex(ValueError, "開始時間不可晚於"):
            db.create_assignment(
                self.teacher_id,
                "Invalid schedule",
                "",
                [question_id],
                student_ids=[self.student_id],
                starts_at="2030-01-02 00:00:00",
                due_at="2030-01-01 00:00:00",
            )

    def test_teacher_can_update_an_owned_assignment_deadline(self):
        self.add_question(self.question())
        question_id = db.list_questions()[0]["id"]
        assignment_id = db.create_assignment(
            self.teacher_id,
            "Scheduled practice",
            "",
            [question_id],
            student_ids=[self.student_id],
        )
        db.update_assignment_deadline(
            self.teacher_id,
            assignment_id,
            "2030-01-01 09:00:00",
            "2030-01-10 17:00:00",
        )
        assignment = db.list_assignments_for_teacher(self.teacher_id)[0]
        self.assertEqual(assignment["starts_at"], "2030-01-01 09:00:00")
        self.assertEqual(assignment["due_at"], "2030-01-10 17:00:00")
        with self.assertRaisesRegex(ValueError, "開始時間不可晚於"):
            db.update_assignment_deadline(
                self.teacher_id,
                assignment_id,
                "2030-01-10 17:00:00",
                "2030-01-01 09:00:00",
            )

    def test_essay_submission_grading_and_feedback(self):
        self.add_question(
            self.question(
                type="essay",
                prompt="Write about your favorite book.",
                options=[],
                answer="",
                points=10,
            )
        )
        essay_id = db.list_questions()[0]["id"]
        result = db.submit_answer(self.student_id, essay_id, "My favorite book is ...")
        self.assertIsNone(result["earned_points"])
        attempt = db.list_essay_attempts(self.teacher_id)[0]
        self.assertIsNone(attempt["earned_points"])
        with self.assertRaisesRegex(ValueError, "介於"):
            db.grade_essay(self.teacher_id, attempt["attempt_id"], 11)
        with self.assertRaisesRegex(ValueError, "教師"):
            db.grade_essay(self.student_id, attempt["attempt_id"], 8)
        db.grade_essay(
            self.teacher_id, attempt["attempt_id"], 8, "Good use of examples."
        )
        self.assertEqual(
            db.list_essay_attempts(self.teacher_id)[0]["feedback"],
            "Good use of examples.",
        )
        self.assertEqual(db.leaderboard()[0]["score"], 8)

    def test_only_teacher_can_view_essay_submissions(self):
        with self.assertRaisesRegex(ValueError, "僅限啟用中的教師"):
            db.list_essay_attempts(self.student_id)

    def test_quiz_scores_are_saved_once_and_student_cannot_change_responses(self):
        self.add_question(self.question(prompt="Select go."))
        self.add_question(
            self.question(
                category="Vocabulary",
                prompt="Select goes.",
                answer="go",
                options=["go", "goes"],
                points=3,
            )
        )
        question_ids = [question["id"] for question in db.list_questions()]
        quiz_id = db.begin_quiz(self.student_id, question_ids)
        quiz = db.get_quiz(quiz_id, self.student_id)
        self.assertEqual([q["id"] for q in quiz["questions"]], question_ids)
        result = db.submit_quiz(
            quiz_id,
            self.student_id,
            {str(question_ids[0]): "goes", str(question_ids[1]): "go"},
        )
        self.assertEqual(result["score"], 8)
        self.assertEqual(result["max_score"], 8)
        self.assertEqual(result["correct_count"], 2)
        with self.assertRaisesRegex(ValueError, "無法重複計分"):
            db.submit_quiz(
                quiz_id,
                self.student_id,
                {str(question_ids[0]): "go", str(question_ids[1]): "go"},
            )
        self.assertEqual(len(db.quiz_results(quiz_id, self.student_id)), 2)
        own_result = db.quiz_results(quiz_id, self.student_id)[0]
        self.assertEqual(own_result["response"], "goes")
        self.assertEqual(own_result["options"], ["go", "goes"])
        self.assertEqual(db.quiz_results(quiz_id, self.other_student_id), [])
        history = db.student_quiz_history(self.student_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["score"], 8)
        self.assertEqual(db.student_progress(self.student_id)["total_score"], 8)

    def test_quiz_essay_waits_for_teacher_grade_and_updates_history(self):
        self.add_question(
            self.question(
                type="essay",
                prompt="Describe a memorable day.",
                options=[],
                answer="",
                points=10,
            )
        )
        question = db.list_questions()[0]
        quiz_id = db.begin_quiz(self.student_id, [question["id"]])
        result = db.submit_quiz(
            quiz_id,
            self.student_id,
            {str(question["id"]): "It was a wonderful day."},
        )
        self.assertEqual((result["score"], result["max_score"]), (0, 10))
        self.assertEqual(db.student_quiz_history(self.student_id)[0]["essays_pending"], 1)
        attempt = db.list_essay_attempts(self.teacher_id)[0]
        db.grade_essay(self.teacher_id, attempt["attempt_id"], 7, "Clear and concise.")
        history = db.student_quiz_history(self.student_id)[0]
        self.assertEqual(history["score"], 7)
        self.assertEqual(history["essays_pending"], 0)

    def test_filtered_assigned_practice_only_selects_assigned_questions(self):
        self.add_question(self.question())
        self.add_question(self.question(prompt="An unassigned question"))
        question_ids = [question["id"] for question in db.list_questions()]
        db.assign_questions(self.teacher_id, self.student_id, [question_ids[0]])
        quiz_id = db.begin_filtered_quiz(
            self.student_id, "Grammar", None, 10, assigned_only=True
        )
        self.assertEqual(
            [question["id"] for question in db.get_quiz(quiz_id, self.student_id)["questions"]],
            [question_ids[0]],
        )

    def test_completed_questions_are_hidden_from_general_practice(self):
        self.add_question(self.question())
        self.add_question(self.question(prompt="A question not yet attempted."))
        questions = db.list_questions()
        first_id, second_id = [question["id"] for question in questions]
        db.submit_answer(self.student_id, first_id, "goes")

        quiz_id = db.begin_filtered_quiz(
            self.student_id, "Grammar", None, 1
        )
        self.assertEqual(
            [
                question["id"]
                for question in db.get_quiz(quiz_id, self.student_id)["questions"]
            ],
            [second_id],
        )
        db.submit_answer(self.student_id, second_id, "goes")
        with self.assertRaisesRegex(ValueError, "沒有可練習的題目"):
            db.begin_filtered_quiz(self.student_id, "Grammar", None, 1)

    def test_password_reset_is_one_time_and_login_history_is_recorded(self):
        db.login("alice", "incorrect")
        db.login("alice", "correct-horse")
        reset_token = db.create_password_reset("alice@example.com")
        self.assertIsNotNone(reset_token)
        self.assertIsNone(db.create_password_reset("missing@example.com"))
        db.reset_student_password(reset_token, "a-replacement-password")
        self.assertIsNone(db.authenticate("alice", "correct-horse"))
        self.assertEqual(
            db.authenticate("alice", "a-replacement-password")["id"], self.student_id
        )
        with self.assertRaisesRegex(ValueError, "無效或已過期"):
            db.reset_student_password(reset_token, "another-password")
        history = db.login_history(self.student_id)
        self.assertEqual([entry["succeeded"] for entry in history], [1, 0])

    def test_teacher_announcements_and_note_moderation(self):
        with self.assertRaisesRegex(ValueError, "教師"):
            db.create_announcement(self.student_id, "Notice", "Content")
        db.create_announcement(self.teacher_id, "Class update", "Practice is open.")
        self.assertEqual(db.list_announcements()[0]["title"], "Class update")

        note_id = db.create_note(self.student_id, "Please review this.")
        db.update_note(note_id, self.student_id, "Updated note.")
        with self.assertRaisesRegex(ValueError, "只能編輯自己的"):
            db.update_note(note_id, self.other_student_id, "Not my note.")
        db.report_note(note_id, self.other_student_id, "Inappropriate content")
        with self.assertRaisesRegex(ValueError, "已經檢舉"):
            db.report_note(note_id, self.other_student_id, "Repeat report")
        reports = db.list_note_reports(self.teacher_id)
        self.assertEqual(reports[0]["note_content"], "Updated note.")
        db.resolve_note_report(self.teacher_id, reports[0]["id"])
        self.assertEqual(db.list_note_reports(self.teacher_id)[0]["status"], "resolved")
        db.delete_note(note_id, self.student_id)
        self.assertEqual(db.list_notes(), [])

    def test_wrong_answer_history_includes_options_and_question_can_be_reported(self):
        question_id = self.add_question(self.question())
        db.submit_answer(self.student_id, question_id, "go")

        attempt = db.student_attempts(self.student_id)[0]
        self.assertEqual(attempt["prompt"], "Choose the correct verb.")
        self.assertEqual(attempt["options"], ["go", "goes"])
        self.assertLess(attempt["earned_points"], attempt["points"])

        with self.assertRaisesRegex(ValueError, "自己作答過"):
            db.report_question(question_id, self.other_student_id, "Wrong option")
        with self.assertRaisesRegex(ValueError, "字元"):
            db.report_question(question_id, self.student_id, " ")
        db.report_question(question_id, self.student_id, "The answer key seems incorrect.")
        with self.assertRaisesRegex(ValueError, "已經回報"):
            db.report_question(question_id, self.student_id, "Duplicate report")

        report = db.list_question_reports(self.teacher_id)[0]
        self.assertEqual(report["question_id"], question_id)
        self.assertEqual(report["options"], ["go", "goes"])
        self.assertEqual(report["reporter_name"], "alice")
        with self.assertRaisesRegex(ValueError, "啟用中的教師"):
            db.list_question_reports(self.student_id)
        db.resolve_question_report(self.teacher_id, report["id"])
        self.assertEqual(
            db.list_question_reports(self.teacher_id)[0]["status"], "resolved"
        )

    def test_daily_backup_creates_a_consistent_copy(self):
        backup_dir = Path(self.temp_dir.name) / "backup"
        old_backup_dir = os.environ.get("CCSS_BACKUP_DIR")
        os.environ["CCSS_BACKUP_DIR"] = str(backup_dir)
        try:
            backup = db.daily_backup()
        finally:
            if old_backup_dir is None:
                os.environ.pop("CCSS_BACKUP_DIR", None)
            else:
                os.environ["CCSS_BACKUP_DIR"] = old_backup_dir
        self.assertTrue(backup.is_file())
        conn = sqlite3.connect(backup)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 3
            )
        finally:
            conn.close()

    def test_shared_notes_and_replies(self):
        note_id = db.create_note(self.student_id, "Good luck with the quiz!")
        db.reply_to_note(note_id, self.other_student_id, "Thank you!")
        notes = db.list_notes()
        self.assertEqual(notes[0]["content"], "Good luck with the quiz!")
        self.assertEqual(notes[0]["replies"][0]["content"], "Thank you!")
        with self.assertRaisesRegex(ValueError, "便利貼內容"):
            db.create_note(self.student_id, " ")


if __name__ == "__main__":
    unittest.main()
