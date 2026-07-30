"""Web UI backend — a thin FastAPI layer over the tutor modules.

Run with:  python -m tutor serve

Design notes
- Grading integrity: rubric key_points and practice model answers never leave
  the server except a model answer revealed after the final practice attempt.
  The pass decision is computed server-side in progress.py.
- Prompt caching: every live call for a unit rebuilds the same byte-identical
  system prefix (persona + unit grounding, 1h TTL), so cache hits work across
  stateless HTTP requests.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import ingest as ingest_mod
from .assess import grade_answers
from .catalog import load_course
from .client import MODEL, cached_system, get_client, usage_cost
from .lesson import TUTOR_PERSONA, build_grounding, check_practice, slower_prompt
from .paths import COURSEPACK_DIR, MATERIAL_DIR, PROGRESS_DIR, WEB_DIR
from .progress import PASS_THRESHOLD, Progress


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    unit_key: str


class PracticeRequest(BaseModel):
    unit_key: str
    segment_index: int
    answer: str
    reveal: bool = False  # client sets true on its final allowed attempt


class HistoryItem(BaseModel):
    role: str
    content: str


class AskRequest(BaseModel):
    unit_key: str
    question: str
    history: list[HistoryItem] = []


class SlowerRequest(BaseModel):
    unit_key: str
    segment_index: int


class AnswerItem(BaseModel):
    criterion_id: str
    student_explanation: str


class AssessRequest(BaseModel):
    unit_key: str
    student: str
    answers: list[AnswerItem]


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(title="AI Course Tutor")
    course = load_course(MATERIAL_DIR)
    client_holder: dict = {}

    def _client():
        if "c" not in client_holder:
            client_holder["c"] = get_client()
        c = client_holder["c"]
        # The SDK only validates auth at request time; fail fast and friendly
        # here instead (covers the streaming endpoints too).
        if not (getattr(c, "api_key", None) or getattr(c, "auth_token", None)):
            raise HTTPException(
                status_code=503,
                detail=(
                    "Claude credentials are not configured. Set the "
                    "ANTHROPIC_API_KEY environment variable and restart the "
                    'server (PowerShell: $env:ANTHROPIC_API_KEY = "sk-ant-...").'
                ),
            )
        return c

    def _unit(unit_key: str):
        unit = course.find_unit(unit_key)
        if unit is None:
            raise HTTPException(status_code=404, detail=f"No unit '{unit_key}'")
        return unit

    def _unit_pack(unit_key: str):
        unit = _unit(unit_key)
        pack = ingest_mod.load_pack(COURSEPACK_DIR, unit)
        if pack is None:
            raise HTTPException(
                status_code=409,
                detail=f"Unit '{unit.title}' has not been prepared yet.",
            )
        return unit, pack

    def _system(unit, pack):
        return cached_system(TUTOR_PERSONA, build_grounding(unit, pack))

    def _stream(system, messages):
        client = _client()  # resolve credentials BEFORE the response starts

        def gen():
            with client.messages.stream(
                model=MODEL, max_tokens=4096, system=system, messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield text
        return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")

    # -- pages ---------------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    # -- course & units --------------------------------------------------------

    @app.get("/api/course")
    def api_course(student: str):
        progress = Progress.load(PROGRESS_DIR, student)
        modules = []
        for module in course.modules:
            units = []
            for u in module.units:
                if progress.has_passed(u.key):
                    status = "passed"
                elif progress.is_unlocked(course, u):
                    status = "unlocked"
                else:
                    status = "locked"
                units.append({
                    "key": u.key,
                    "title": u.title,
                    "ingested": ingest_mod.pack_path(COURSEPACK_DIR, u).exists(),
                    "status": status,
                    "best": progress.best_score(u.key),
                    "attempts": len(progress.attempts(u.key)),
                })
            modules.append({"key": module.key, "title": module.title, "units": units})
        return {
            "title": course.title,
            "name": course.name,
            "student": student,
            "threshold": PASS_THRESHOLD,
            "modules": modules,
        }

    @app.get("/api/unit/{unit_key}")
    def api_unit(unit_key: str):
        unit, pack = _unit_pack(unit_key)
        return {
            "key": unit.key,
            "title": unit.title,
            "overview": pack.overview,
            "objectives": pack.learning_objectives,
            "definitions": [d.model_dump() for d in pack.key_definitions],
            "study_guide": pack.study_guide.model_dump(),
            "module_breakdown": [m.model_dump() for m in pack.module_breakdown],
            "segments": [
                {
                    "title": s.title,
                    "content": s.content,
                    # question + hint only; the model answer stays server-side
                    "practice": (
                        {"question": s.practice.question, "hint": s.practice.hint}
                        if s.practice else None
                    ),
                }
                for s in pack.segments
            ],
            # concepts only; key_points stay server-side
            "rubric": [{"id": c.id, "concept": c.concept} for c in pack.rubric],
        }

    @app.post("/api/ingest")
    def api_ingest(req: IngestRequest):
        unit = _unit(req.unit_key)
        if ingest_mod.pack_path(COURSEPACK_DIR, unit).exists():
            return {"ok": True, "already": True}
        pack, summary, cost = ingest_mod.ingest_unit(_client(), unit, COURSEPACK_DIR)
        return {
            "ok": True,
            "already": False,
            "segments": len(pack.segments),
            "usage": summary,
            "cost": round(cost, 2),
        }

    # -- live lesson calls -------------------------------------------------------

    @app.post("/api/practice")
    def api_practice(req: PracticeRequest):
        unit, pack = _unit_pack(req.unit_key)
        if not (0 <= req.segment_index < len(pack.segments)):
            raise HTTPException(status_code=400, detail="Bad segment index")
        pq = pack.segments[req.segment_index].practice
        if pq is None:
            raise HTTPException(status_code=400, detail="Segment has no practice question")
        check = check_practice(_client(), pq, req.answer)
        result = {"verdict": check.verdict, "feedback": check.feedback}
        if req.reveal and check.verdict != "correct":
            result["model_answer"] = pq.model_answer
        return result

    @app.post("/api/ask")
    def api_ask(req: AskRequest):
        unit, pack = _unit_pack(req.unit_key)
        history = [
            {"role": h.role, "content": h.content}
            for h in req.history if h.role in ("user", "assistant")
        ][-10:]
        messages = history + [{"role": "user", "content": req.question}]
        return _stream(_system(unit, pack), messages)

    @app.post("/api/slower")
    def api_slower(req: SlowerRequest):
        unit, pack = _unit_pack(req.unit_key)
        if not (0 <= req.segment_index < len(pack.segments)):
            raise HTTPException(status_code=400, detail="Bad segment index")
        seg = pack.segments[req.segment_index]
        messages = [{"role": "user", "content": slower_prompt(req.segment_index, seg.title)}]
        return _stream(_system(unit, pack), messages)

    # -- assessment ---------------------------------------------------------------

    @app.post("/api/assess")
    def api_assess(req: AssessRequest):
        unit, pack = _unit_pack(req.unit_key)
        progress = Progress.load(PROGRESS_DIR, req.student)
        if not progress.is_unlocked(course, unit):
            raise HTTPException(status_code=403, detail="Unit is locked")

        given = {a.criterion_id: a.student_explanation for a in req.answers}
        answers = [
            {
                "criterion_id": c.id,
                "concept": c.concept,
                "student_explanation": given.get(c.id, "").strip() or "(no answer)",
            }
            for c in pack.rubric
        ]
        report, score, per_criterion, usage = grade_answers(_client(), pack, answers)
        passed = score >= PASS_THRESHOLD
        progress.record_attempt(unit.key, score, passed, per_criterion)

        by_id = {g.criterion_id: g for g in report.criterion_grades}
        criteria = []
        for i, crit in enumerate(pack.rubric):
            grade = by_id.get(crit.id) or (
                report.criterion_grades[i] if i < len(report.criterion_grades) else None
            )
            criteria.append({
                "concept": crit.concept,
                "points": per_criterion[crit.id],
                "feedback": grade.feedback if grade else "(not graded)",
                "missing_points": grade.missing_points if grade else [],
            })

        next_unit = progress.current_unit(course)
        summary, cost = usage_cost(usage)
        return {
            "score": score,
            "passed": passed,
            "threshold": PASS_THRESHOLD,
            "overall_feedback": report.overall_feedback,
            "criteria": criteria,
            "next_unit": (
                {"key": next_unit.key, "title": next_unit.title} if next_unit else None
            ),
            "usage": summary,
            "cost": round(cost, 2),
        }

    return app


def serve(port: int = 8787) -> None:
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
