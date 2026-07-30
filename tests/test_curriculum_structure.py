import unittest

from tutor.lesson import build_grounding
from tutor.models import UnitPack
from tutor.catalog import Unit


class CurriculumStructureTests(unittest.TestCase):
    def test_unit_pack_accepts_study_guide_and_module_breakdown(self) -> None:
        pack = UnitPack.model_validate({
            "overview": "Overview",
            "learning_objectives": ["Learn the idea"],
            "key_definitions": [],
            "segments": [],
            "rubric": [],
            "study_guide": {
                "summary": "A simple overview",
                "why_it_matters": "It unlocks the next idea",
                "prerequisites": ["Prior topic"],
                "connects_to_previous": ["Earlier topic"],
                "mastery_targets": ["Define the concept"],
                "common_misconceptions": ["Misconception"],
                "worked_example": "Example",
                "next_steps": ["Practice"],
            },
            "module_breakdown": [
                {
                    "title": "First step",
                    "summary": "Start here",
                    "concepts": ["Concept A"],
                }
            ],
        })

        self.assertEqual(pack.study_guide.summary, "A simple overview")
        self.assertEqual(pack.module_breakdown[0].title, "First step")

    def test_build_grounding_includes_study_guide_material(self) -> None:
        unit = Unit(
            key="demo_unit",
            title="Demo unit",
            module_key="demo_module",
            pdf_paths=[],
        )
        pack = UnitPack.model_validate({
            "overview": "Overview",
            "learning_objectives": ["Learn the idea"],
            "key_definitions": [],
            "segments": [],
            "rubric": [],
            "study_guide": {
                "summary": "A simple overview",
                "why_it_matters": "It unlocks the next idea",
                "prerequisites": ["Prior topic"],
                "connects_to_previous": ["Earlier topic"],
                "mastery_targets": ["Define the concept"],
                "common_misconceptions": ["Misconception"],
                "worked_example": "Example",
                "next_steps": ["Practice"],
            },
            "module_breakdown": [
                {
                    "title": "First step",
                    "summary": "Start here",
                    "concepts": ["Concept A"],
                }
            ],
        })

        grounding = build_grounding(unit, pack)
        self.assertIn("## Study guide", grounding)
        self.assertIn("## Module breakdown", grounding)
        self.assertIn("Prior topic", grounding)
        self.assertIn("It unlocks the next idea", grounding)
        self.assertIn("Define the concept", grounding)


if __name__ == "__main__":
    unittest.main()
