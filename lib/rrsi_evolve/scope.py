"""Harness scope: what the harness is for, and so what counts as overfitting.

"general" (default): the harness should help on unfamiliar tasks in unfamiliar
repositories, so anything specific to the suite's repository is leakage.

"repo": the harness is installed in ONE repository and only runs there; the
held-out tasks are other tasks in that same repository. Facts about the
repository that hold for every task in it (its team's conventions) then
transfer to held-out tasks and are what the harness is for. Task-specific
answers are still leakage.
"""

SCOPES = ("general", "repo")

SKILL_AMENDMENT = """

## Harness scope: repo (this amends the litmus test and rules 2 and 4 above)

This harness is installed in ONE repository and only ever runs there. The
held-out tasks are other tasks in the same repository. Knowledge about THIS
REPOSITORY that holds for every task in it therefore transfers, and writing it
down is the point of the harness, not overfitting. When the traces and the
verifier output show the agent could not have known a team rule, record the
rule: which files are generated or must never be hand-edited, where change
records go and their exact format, which artifacts each kind of change needs,
naming and placement conventions, which commands are safe or forbidden, and
the team's required report format including its exact heading words.

Repo-scope litmus test: "would a new engineer joining THIS team need to be told
this on day one, whichever ticket they pick up?" If yes, it is legitimate.

Still forbidden (rule 2, applied to tasks rather than to the repository):
task ids, ticket numbers, the specific feature, value or file a single task
asks for, expected outputs, and anything that branches on which task is
running. Rule 3 (never touch the checker or evaluation plumbing) is unchanged.

Missing knowledge is fixed by stating it: for a rule the agent could not have
known, a short "Team conventions" section in CLAUDE.md (one line per rule, with
the reason when the evidence shows it) IS the mechanism; rule 4's preference
for mechanism over wording applies to failures of process, not of knowledge.
Write each rule as the team would, not as the verifier phrases a failure.
"""

CRITIC_AMENDMENT = """

HARNESS SCOPE: repo. This harness is installed in one repository and is
measured on held-out tasks from that same repository, with the verifier
playing the role of the team's reviewer. Under rules 1 and 3, repository-wide
conventions are NOT leakage or grader gaming: generated files that must not be
edited, where change records go and their format, artifacts required per kind
of change, naming and placement, safe or forbidden commands, and the team's
report format with its exact heading words. Still REJECT task ids, ticket
numbers, values or features that only one task asks for, expected outputs,
and branching on which task is running."""


def check(scope: str) -> str:
    if scope not in SCOPES:
        raise ValueError(f"harness_scope must be one of {SCOPES}, got {scope!r}")
    return scope


def skill(skill_md: str, scope: str) -> str:
    return skill_md + SKILL_AMENDMENT if check(scope) == "repo" else skill_md


def critic(system: str, scope: str) -> str:
    return system + CRITIC_AMENDMENT if check(scope) == "repo" else system


def banner(scope: str, explicit: bool) -> str:
    """One line for round/run: which scope, and what that means for installing."""
    if scope == "repo":
        return ("[rrsi] harness scope: repo (the harness may record this repository's "
                "conventions: install it in this repository's .claude/ and CLAUDE.md, "
                "not in ~/.claude)")
    if explicit:
        return ("[rrsi] harness scope: general (for a harness shared across projects, "
                "e.g. ~/.claude or a subagent prompt)")
    return ("[rrsi] harness scope: general (harness_scope is not set in rrsi.json; if this "
            "harness serves only this repository, set \"harness_scope\": \"repo\" so it "
            "can learn the repository's conventions)")
