# Seam skill golden cases

`cases.json` holds golden cases for `skills/seam/SKILL.md`. They are manual,
agent-run checks: nothing here calls ComfyUI, a GPU or an LLM in CI.

Each case has:

- `user_request`: what the user says.
- `situation`: what the Seam tools would return (preset ids are in backticks).
- `must`: regexes the agent's answer or plan has to match.
- `must_not`: regexes it must not match.

## How to run

1. Start Claude with the `seam` skill loaded.
2. Give it a case: the `user_request`, plus the `situation` as the tool results.
3. Ask for its plan and final answer, then test every `must` and `must_not`
   regex against that text (case-insensitive flags are inline in each regex).
4. A case passes when all `must` match and no `must_not` matches.

`uv run pytest tests/test_skill_evals.py` only checks that the cases file is
well formed and that SKILL.md still has the anchors the cases rely on.
