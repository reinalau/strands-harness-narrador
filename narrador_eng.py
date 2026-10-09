"""
mini-Sherpa — Story narrator built with Strands Agents Harness.

Everything follows DOCUMENTED patterns of Strands harness / the Strands Harness SDK:

- Subagents = "Agents as Tools" (doc: Delegate to subagents). `continuity` and
  `structure` are raw `strands.Agent`s with name/description/system_prompt, passed in
  via `.as_tool()`.
  Ref: https://strandsagents.com/docs/user-guide/harness/configure/subagents/
- The librarian uses STRUCTURED OUTPUT (doc: Return structured output): a Nova agent
  with `structured_output_model=Bible` (Pydantic), exposed as a `@tool`. It returns
  validated JSON instead of free-form text.
  Ref: https://strandsagents.com/docs/user-guide/sdk/agents/structured-output/
- Human-in-the-loop = turn-based chat (NOT an approval gate). The agent writes one
  chapter, saves it, and ENDS its turn; it never chains the next one. Control returns to
  the person, who reads/edits the files and, on their next message, asks to "review" or
  "continue". The agent re-reads from disk before acting. It is the natural flow of the
  chat loop; no `interventions` needed.
- Models are instantiated with GeminiModel / BedrockModel (doc: strands.models.*). A raw
  Agent needs a Model instance; the "google/..." / "bedrock/..." strings are resolved
  ONLY by create_harness().
  Ref: https://strandsagents.com/docs/api/python/strands.models.gemini/

TWO MODELS:
  - Orchestrator (writes the prose and to disk): Gemini 3.5 Flash.
  - Subagents (analysis only, never touch disk): Nova Lite on Bedrock, cheaper.
"""

import os
import sys

# Windows: the console uses cp1252 and crashes when printing non-ascii characters
# (accents, emojis). We force UTF-8 so the output does not kill the process.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Always work from this script's folder, so relative paths (story/, bible.json,
# state.json) resolve here.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()  # loads GEMINI_API_KEY

from typing import Optional

from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from strands_harness import create_harness

# TWO MODELS:
#  - Orchestrator (writes the prose and to disk): Gemini 3.5 Flash. Handles Windows
#    paths well (unlike Nova) and has its own free-tier quota.
#  - Subagents (analysis only, never touch disk): Nova Lite on Bedrock, cheaper.
GEMINI_MODEL_ID = "gemini-3.5-flash"
EFFORT = "off"

NOVA_MODEL_ID = "us.amazon.nova-lite-v1:0"


def nova_model():
    """Nova Lite (Bedrock) model instance for the analysis-only subagents.

    Doc: strands.models.bedrock. A raw Agent needs a Model instance; the "bedrock/..."
    string is only understood by create_harness(). region_name defaults to AWS_REGION or
    us-west-2; here we pin it to the configured region (us-east-1).
    """
    return BedrockModel(
        model_id=NOVA_MODEL_ID,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


# ---------------------------------------------------------------------------
# Story bible schema (the librarian's structured output).
#
# Doc: Return structured output. A Pydantic model is passed as structured_output_model
# and read back validated from result.structured_output. For the librarian this replaces
# the fragile "return JSON without ```" with a guaranteed-valid object.
# Ref: https://strandsagents.com/docs/user-guide/sdk/agents/structured-output/
# ---------------------------------------------------------------------------


class Meta(BaseModel):
    title: str = Field(description="Story title")
    genre: str = Field(description="Genre")
    tone: str = Field(description="Tone")
    point_of_view: str = Field(description="Narrative point of view")
    last_chapter: int = Field(description="Number of the last closed chapter")


class Character(BaseModel):
    name: str
    role: str
    goal: str
    since_chapter: int = Field(description="Chapter in which it first appears")


class Relationship(BaseModel):
    a: str = Field(description="One character")
    b: str = Field(description="The other character")
    type: str = Field(description="Relationship type")
    since_chapter: int


class WhoKnowsWhat(BaseModel):
    character: str
    knows: str = Field(description="What the character knows")
    since_chapter: int


class OpenThread(BaseModel):
    description: str
    opened_in_chapter: int
    closed_in_chapter: Optional[int] = Field(
        default=None, description="Chapter in which it closes, or null if still open"
    )


class Object(BaseModel):
    name: str
    description: str
    since_chapter: int


class TimelineEntry(BaseModel):
    chapter: int
    events: list[str]


class Bible(BaseModel):
    """Story bible. It is the librarian's structured_output."""

    meta: Meta
    characters: list[Character]
    relationships: list[Relationship]
    who_knows_what: list[WhoKnowsWhat]
    open_threads: list[OpenThread]
    objects: list[Object]
    timeline: list[TimelineEntry]


# ---------------------------------------------------------------------------
# Subagents = Agents as Tools (documented pattern). Raw Agent + as_tool().
# They are ANALYSIS-ONLY: no disk tools; they receive text and return text. The
# orchestrator is the only one that persists.
# ---------------------------------------------------------------------------

continuity = Agent(
    name="continuity",
    description=(
        "Reviews a chapter's continuity against the story bible. Pass it the contents "
        "of bible.json and the chapter text. Returns a report of contradictions, "
        "impossible knowledge, and dropped threads."
    ),
    model=nova_model(),
    system_prompt=(
        "You are a narrative continuity reviewer, writing in English. You receive the "
        "story bible (JSON) and a chapter's text. Report ONLY problems, in a concrete "
        "and actionable way: (1) contradictions with the bible (characters, "
        "relationships, objects, timeline); (2) impossible knowledge (a character knows "
        "something they should not know yet); (3) open threads that are abandoned. Do "
        "not rewrite the chapter; only list the problems. If there are none, say "
        "'No continuity issues'."
    ),
)

structure = Agent(
    name="structure",
    description=(
        "Audits the structure and pacing of a short story or chapter. Pass it the mode "
        "(short story/novel), the chapter's position if applicable, and the text. "
        "Returns a report on narrative stages and pacing."
    ),
    model=nova_model(),
    system_prompt=(
        "You are a narrative structure and pacing editor, writing in English. Audit the "
        "text and report ONLY what fails or can be improved.\n"
        "SHORT STORY: check the opening, inciting incident, rising tension in the "
        "middle, a clear climax, and an ending that changes the initial balance.\n"
        "NOVEL CHAPTER: check the character's initial goal, the knot (problem or "
        "revelation), and an ending on a HOOK/cliffhanger (not a closure). Consider its "
        "position across the three acts (setup/confrontation/resolution).\n"
        "PACING (always): show, don't tell; dramatize the key moments and summarize the "
        "passage of time; enter late and leave early in each scene.\n"
        "Do not rewrite the text; list concrete observations. If it is fine, say "
        "'Structure and pacing are correct'."
    ),
)

# The librarian uses STRUCTURED OUTPUT (doc: Return structured output). Instead of
# returning "JSON without ```" and hoping, the Nova agent produces a Bible object
# validated by Pydantic. It is exposed to the orchestrator as a @tool: it receives the
# current bible and the chapter, and returns the validated JSON (string) that the
# orchestrator writes to disk.
_librarian_agent = Agent(
    model=nova_model(),
    system_prompt=(
        "You are the story's librarian, writing in English. You receive the current "
        "bible (JSON, which may be empty) and a chapter's text. Update the bible: keep "
        "what was already there and add what is new in the chapter. Relationships are a "
        "list of edges. Set 'since_chapter' where appropriate."
    ),
)


@tool
def librarian(current_bible: str, chapter_text: str, chapter_number: int) -> str:
    """Updates the story bible with a new chapter and returns the validated JSON.

    Args:
        current_bible: Current contents of bible.json (or "" / "{}" if it is chapter 1).
        chapter_text: Full text of the chapter just written.
        chapter_number: Chapter number (1, 2, ...).

    Returns:
        The updated bible JSON, validated against the Bible schema.
    """
    prompt = (
        f"Current bible:\n{current_bible or '(empty, this is the first chapter)'}\n\n"
        f"Chapter {chapter_number}:\n{chapter_text}\n\n"
        "Return the complete, updated bible."
    )
    result = _librarian_agent(prompt, structured_output_model=Bible)
    bible: Bible = result.structured_output
    return bible.model_dump_json(indent=2)


# ---------------------------------------------------------------------------
# Orchestrator prompt (the narrator)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a story narrator writing in English, and the ORCHESTRATOR of a set of agents.
Your output is ONLY text in English. Do not use shell or web. Your tools are file tools
(read, write, edit) and three subagents you can call: "continuity", "structure" and
"librarian". ONLY YOU write to disk; the subagents only analyze and return text to you.

INITIAL INTERVIEW
If format (short story or novel), genre, tone, characters or premise are missing, ask
for everything in ONE short, numbered message. If the user does not decide something,
propose a value and move on.

NARRATIVE STRUCTURE (follow it while writing)
Short story: opening (may be in medias res) -> inciting incident -> knot (rising
tension) -> climax -> ending that changes the initial balance. Fast, direct pacing.
Novel (macro): Act 1 setup (~20-25%, ends on a turning point) -> Act 2 confrontation
(~50%, with a midpoint point of no return) -> Act 3 resolution (~20-25%, climax and
closure). Each chapter (micro) is a mini-story: initial goal -> knot (problem or
revelation) -> ending on a HOOK/cliffhanger, not a closure.
Always: show, don't tell; dramatize the key moments and summarize the passage of time;
enter late and leave early in each scene.

SHORT STORY MODE
One chapter of 1200 to 2500 words. Deliver the text in the chat. If the person wants to
edit it and ask for a review, save it to story/story.md.

NOVEL MODE - for each chapter, in this order:
1. If bible.json exists, READ IT from disk before writing. If it does not exist, this is
   chapter 1.
2. Write the chapter (800 to 1500 words) respecting the bible and the structure.
3. DELEGATE to "continuity" (bible + new chapter): have it report contradictions,
   impossible knowledge, and dropped threads.
4. DELEGATE to "structure": have it audit the chapter's stages and pacing.
5. Apply the corrections that apply (you decide; the subagents only report).
6. Save the chapter to story/chapter-NN.md.
7. DELEGATE to "librarian": have it RETURN the updated bible.json. You write that JSON
   to bible.json.
8. Update state.json (last_chapter, and the chapter with n, title, file, status
   "generated", cliffhanger).

HUMAN-IN-THE-LOOP (one chapter per turn; stop and wait)
Write ONE chapter (or the short story) per turn. After saving it, STOP: tell the person
they can read and manually edit the files (the .md and/or bible.json), and that whenever
they want they can ask you to "review" or "continue". NEVER start the next chapter on
your own; wait until the person explicitly asks for it.
When asked to REVIEW: RE-READ the files from disk (do not assume your context is the
latest; the person may have edited them), delegate to "continuity" and "structure" over
what is on disk, report, and update the chapter's status to "reviewed" in state.json.
When asked to CONTINUE: RE-READ bible.json and the last chapter from disk before writing
the next one.

CHAPTER REPORT
If the person asks for it, read state.json and return an enumeration with the N and
title of each chapter (and, if useful, its status or cliffhanger).

ERROR HANDLING (honesty on failure)
If a file tool fails, say so verbatim and show the full text in the chat. NEVER claim
you saved something if the write failed.

DELIVERY
Deliver only the story, with a title and "Chapter N" where appropriate.
"""

# ---------------------------------------------------------------------------
# Orchestrator: harness with a narrowed tool set and subagents as tools.
#
# The "human-in-the-loop" is NOT an approval gate: it is the turn-based chat. The agent
# writes a chapter, saves it, and ends its turn (the prompt asks for this). Control
# returns to the person, who reads/edits the files and, on their next message, decides to
# "review" or "continue". No `interventions` needed for this.
# ---------------------------------------------------------------------------


def build_narrator():
    return create_harness(
        model=f"google/{GEMINI_MODEL_ID}",  # orchestrator on Gemini; the harness resolves the string
        effort=EFFORT,
        instructions=SYSTEM_PROMPT,
        # File read/write/edit only. Everything else, off.
        builtin_tools={
            "*": False,
            "read": True,
            "write": True,
            "edit": True,
            # explicitly disabled: shell, web_fetch, web_search,
            # programmatic_tool_caller, and the built-in generalist subagent.
        },
        # Delegation tools:
        #  - continuity / structure: Agents as Tools (reviewers, return a report).
        #  - librarian: a @tool with structured output (returns a validated bible).
        tools=[continuity.as_tool(), structure.as_tool(), librarian],
        # Long-term memory off: state lives in files and the session.
        memory=False,
        # Caching and context management on automatic.
        caching="auto",
        context_manager="auto",
        # Session persisted with a stable id per story (resumable).
        session={"id": "mini-sherpa-story-1"},
        # Silences the model's console streaming (raw thinking, "Tool #N").
        # Only what main() prints is shown. Passed straight through to the SDK Agent.
        callback_handler=None,
    )


def main():
    print("=== mini-Sherpa — Story narrator ===")
    print("Type your request. The narrator writes ONE chapter per turn and stops;")
    print("then you can edit the files and ask it to 'review' or 'continue'.")
    print("Commands: 'exit' to quit.\n")

    # Turn-based chat: the agent never chains chapters on its own; it always waits for
    # your next message. That is the human-in-the-loop pause.
    agent = build_narrator()

    while True:
        try:
            user = input("✍ ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nUntil next time.")
            break

        if not user:
            continue
        if user.lower() in {"exit", "quit", "salir"}:
            print("Until next time.")
            break

        result = agent(user)
        print("\n📖 Narrator:", str(result), "\n")


if __name__ == "__main__":
    main()
