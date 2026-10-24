"""Status parsing never creates camera/research work or consumes result consent."""
import unittest

from task_status import explicit_retake, parse_status_query


class TaskStatusParserTests(unittest.TestCase):
    def test_camera_progress_queries(self):
        for text in ("Check out the identification process.", "How is the identification going?",
                     "How is identification progressing?",
                     "Can you check the photo status?", "Is the photo ready yet?",
                     "识别到哪一步了", "现在照片识别进度如何？"):
            with self.subTest(text=text):
                self.assertEqual(parse_status_query(text), {"scope": "camera"})

    def test_generic_queries(self):
        for text in ("Any progress?", "What is the status?", "Is it done yet?",
                     "Any results?", "Any results yet?", "Do you have a result yet?",
                     "How's it going?", "查得怎么样了", "任务进度", "有结果了吗？", "有结果吗？"):
            with self.subTest(text=text):
                self.assertEqual(parse_status_query(text), {"scope": "auto"})

    def test_explicit_ids_and_order(self):
        self.assertEqual(parse_status_query("R1进行到哪一步了"), {"scope": "investigations", "id": "R1"})
        self.assertEqual(parse_status_query("What is R 12's status?"), {"scope": "investigations", "id": "R12"})
        self.assertEqual(parse_status_query("How is the first investigation going?"),
                         {"scope": "investigations", "selection": "first"})
        self.assertEqual(parse_status_query("最新调查的进度如何"), {"scope": "investigations", "selection": "latest"})
        self.assertEqual(parse_status_query("How is the research going?"), {"scope": "investigations"})
        self.assertEqual(parse_status_query("Research progress?"), {"scope": "investigations"})
        self.assertEqual(parse_status_query("Where are we with the identification?"), {"scope": "camera"})
        self.assertIsNone(parse_status_query("What is the status of R1 and R2?"))

    def test_all_scopes(self):
        self.assertEqual(parse_status_query("What is the status of all tasks?"), {"scope": "all", "selection": "all"})
        self.assertEqual(parse_status_query("How are all investigations going?"),
                         {"scope": "investigations", "selection": "all"})
        self.assertEqual(parse_status_query("所有任务做完了吗？"), {"scope": "all", "selection": "all"})
        self.assertEqual(parse_status_query("What is the status of the photo and the investigation?"), {"scope": "all"})

    def test_controls_result_requests_and_consent_are_not_status(self):
        for text in ("Take another photo.", "Look again.", "Tell me the result.", "Explain R1.",
                     "Yes.", "Okay.", "Cancel R1 and tell me its status.",
                     "Retry the identification.", "Start a new investigation; any progress?",
                     "How is the identification going, and take another photo?",
                     "Please research the bird's diet.", "重新拍照看看识别进度", "取消R1，进度如何", "解释一下R1结果",
                     "告诉我识别结果", "识别结果怎么样？"):
            with self.subTest(text=text):
                self.assertIsNone(parse_status_query(text))

    def test_nature_questions_are_not_status(self):
        for text in ("How does photosynthesis work?", "What does it eat?", "What is that?",
                     "What is the conservation status of the bird in this photo?",
                     "How does image identification work?", "Is it ready to eat?",
                     "这个鸟吃什么？", "如何识别一种鸟？", "照片识别是怎么工作的？"):
            with self.subTest(text=text):
                self.assertIsNone(parse_status_query(text))

    def test_retake_requires_camera_action_not_new_topic(self):
        for text in ("Take another photo.", "Can you take a new picture?", "Can you retake the photo?", "Look again.",
                     "Look at it again.", "Switch to a different subject.", "New subject.",
                     "Look at another animal.", "Check out another bird.", "Identify a different animal.",
                     "Look at a different bird.", "Look at another plant.",
                     "再拍一张", "重新拍照", "换一个观察对象", "再看一眼", "看看另一个动物", "换一个动物"):
            with self.subTest(text=text):
                self.assertTrue(explicit_retake(text))
        for text in ("What is different about this new bird?", "Any new progress?",
                     "What does the new species eat?", "Do not take another photo.", "不要重新拍照",
                     "What does the new subject eat?", "What is a retake?",
                     "Do not identify another animal.", "不要换一个动物",
                     "Retake is not needed.", "No need for a retake.", "No retake needed.", "重拍没必要",
                     "Do I need to retake the photo?", "Should I look again?", "Does this need a new photo?",
                     "调查不同物种的食性", "换一种识别方法"):
            with self.subTest(text=text):
                self.assertFalse(explicit_retake(text))

if __name__ == "__main__":
    unittest.main()
