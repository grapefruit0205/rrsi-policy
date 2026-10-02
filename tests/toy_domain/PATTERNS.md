# Toy Pattern Library (reference, not an allowlist)

Mechanism families for standing-instruction harnesses, with general traps.

## 1. Bounded stop gates

"Finish the change, verify once, then stop" standing rules keep runs from
polishing past the rewarded deliverable. Generic trap: a stop that never
re-opens finished work must still allow ONE verification pass, or runs end
before the deliverable matches the prompt.

## 2. General-practice rule files

Rules like "answer verification questions with the general rule that decides
them, then stop" teach the agent to cite the deciding practice instead of
re-deriving it. Generic trap: a rule that names a specific deliverable is
task-specific and rejects; a rule that names the KIND of question is general.

## 3. Smallest-change discipline

"Make the smallest change that satisfies the prompt" keeps trials fast and
keeps currently passing tasks passing. Generic trap: an extra file the prompt
did not ask for is a regression source.

## 4. Self-confirmation before finishing

"Re-read the prompt and confirm the deliverable matches every clause" catches
clause misses on any task. Generic trap: an unbounded re-read loop ("keep
checking until sure") never terminates; bound it to one pass.

## 5. Skills as progressive disclosure

A skill (YAML frontmatter name + description, body = the procedure) costs
nothing until invoked and gives a recurring procedure an explicit home.
Generic trap: a purely advisory skill washes out; the body should end in a
runnable check.
