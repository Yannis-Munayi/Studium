"""Interactive lesson player — the "YouTube-style" learning experience.

The lesson script was generated once at ingestion and is served from disk, so
navigation is instant and consistent:

  [Enter] next segment          (play)
  b       previous segment      (rewind)
  r       show segment again    (replay)
  s       re-explain simpler    (slow down — live Claude call)
  a       ask a question        (live Claude call, remembers the conversation)
  d       key definitions       o  learning objectives
  q       quit lesson

Live calls share one cached system prefix (persona + full unit content), so
every question after the first reads the unit context at ~10% input price.
"""

from __future__ import annotations

from .catalog import Unit
from .client import MODEL, cached_system, get_client
from .models import AnswerCheck, PracticeQuestion, UnitPack
from .ui import console, panel, read_multiline

TUTOR_PERSONA = """\
You are a patient university tutor. Teach from the unit content provided in
this system prompt and nothing else; if a question falls outside the unit, say
so and point the student back to the material. Explain step by step, prefer
small concrete examples, and never assume knowledge the unit has not covered
yet. Keep answers focused — usually under 250 words.
"""

CHECK_SYSTEM = """\
You grade a single practice-question answer against a model answer. Judge
substance, not wording. An answer that captures the model answer's key idea is
"correct"; one that captures part of it is "partially_correct"; anything else,
including restating the question or "I don't know", is "incorrect". Feedback:
1-3 sentences, encouraging, and if the answer is not correct, point at what is
missing without giving the full model answer away.
"""


def build_grounding(unit: Unit, pack: UnitPack) -> str:
    lines = [f"# Unit: {unit.title}", "", pack.overview, "", "## Key definitions"]
    lines += [f"- **{d.term}**: {d.definition}" for d in pack.key_definitions]
    lines.append("\n## Lesson content")
    for i, seg in enumerate(pack.segments, 1):
        lines += [f"\n### Segment {i}: {seg.title}", seg.content]
    return "\n".join(lines)


def check_practice(client, pq: PracticeQuestion, answer: str) -> AnswerCheck:
    """Grade one practice answer. Shared by the terminal player and web app."""
    response = client.messages.parse(
        model=MODEL,
        max_tokens=1024,
        system=CHECK_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Question: {pq.question}\n\n"
                f"Model answer: {pq.model_answer}\n\n"
                f"Student answer: {answer}"
            ),
        }],
        output_format=AnswerCheck,
    )
    return response.parsed_output


def slower_prompt(index: int, segment_title: str) -> str:
    return (
        f"Re-explain segment {index + 1} ('{segment_title}') more slowly and "
        "simply: smaller steps, one everyday analogy, and finish with a "
        "one-sentence takeaway."
    )


class LessonSession:
    def __init__(self, unit: Unit, pack: UnitPack):
        self.unit = unit
        self.pack = pack
        self.client = get_client()
        self.system = cached_system(TUTOR_PERSONA, build_grounding(unit, pack))
        self.history: list[dict] = []  # rolling Q&A history for `a`

    # -- live calls -----------------------------------------------------------

    def _stream_reply(self, user_text: str, keep_history: bool) -> None:
        messages = self.history + [{"role": "user", "content": user_text}]
        console.print()
        with self.client.messages.stream(
            model=MODEL, max_tokens=4096, system=self.system, messages=messages,
        ) as stream:
            for text in stream.text_stream:
                console.print(text, end="")
            final = stream.get_final_message()
        console.print("\n")
        if keep_history:
            reply = "".join(b.text for b in final.content if b.type == "text")
            self.history += [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": reply},
            ]
            self.history = self.history[-10:]  # keep the tail; prefix cache covers the rest

    def explain_slower(self, index: int) -> None:
        seg = self.pack.segments[index]
        self._stream_reply(slower_prompt(index, seg.title), keep_history=False)

    def ask(self, question: str) -> None:
        self._stream_reply(question, keep_history=True)

    # -- practice -------------------------------------------------------------

    def run_practice(self, pq: PracticeQuestion) -> None:
        panel(pq.question, "Practice", style="yellow")
        for attempt in (1, 2):
            answer = read_multiline("Your answer")
            if not answer:
                console.print("[dim]Skipped.[/dim]")
                return
            check = check_practice(self.client, pq, answer)
            color = {"correct": "green", "partially_correct": "yellow",
                     "incorrect": "red"}[check.verdict]
            console.print(f"[{color}]{check.verdict.replace('_', ' ')}[/{color}] — {check.feedback}")
            if check.verdict == "correct" or attempt == 2:
                if check.verdict != "correct":
                    panel(pq.model_answer, "Model answer", style="green")
                return
            console.print(f"[dim]Hint: {pq.hint}[/dim]  Try once more.")

    # -- main loop --------------------------------------------------------------

    def show_segment(self, index: int) -> None:
        seg = self.pack.segments[index]
        panel(seg.content, f"[{index + 1}/{len(self.pack.segments)}] {seg.title}")

    def run(self) -> bool:
        """Play the lesson. Returns True if the student reached the end."""
        panel(self.pack.overview, f"Unit: {self.unit.title}", style="magenta")
        panel(
            "\n".join(f"- {o}" for o in self.pack.learning_objectives),
            "Learning objectives", style="magenta",
        )

        index = 0
        practiced: set[int] = set()
        while index < len(self.pack.segments):
            self.show_segment(index)
            seg = self.pack.segments[index]
            if seg.practice and index not in practiced:
                practiced.add(index)
                self.run_practice(seg.practice)

            while True:
                console.print(
                    "[dim][Enter] next · b back · r replay · s slower · "
                    "a ask · d definitions · o objectives · q quit[/dim]"
                )
                cmd = input("lesson> ").strip()
                if cmd == "":
                    index += 1
                    break
                if cmd == "b":
                    index = max(0, index - 1)
                    break
                if cmd == "r":
                    self.show_segment(index)
                elif cmd == "s":
                    self.explain_slower(index)
                elif cmd == "a":
                    question = read_multiline("Your question")
                    if question:
                        self.ask(question)
                elif cmd == "d":
                    panel(
                        "\n".join(f"- **{d.term}**: {d.definition}"
                                  for d in self.pack.key_definitions),
                        "Key definitions",
                    )
                elif cmd == "o":
                    panel(
                        "\n".join(f"- {o}" for o in self.pack.learning_objectives),
                        "Learning objectives",
                    )
                elif cmd == "q":
                    return False
                else:
                    console.print("[dim]Unknown command.[/dim]")

        console.print("[bold green]End of lesson.[/bold green]")
        return True
