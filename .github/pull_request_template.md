<!-- Write N/A if a section does not apply. -->

## Summary

<!-- What problem does this solve, and why this way? One to three sentences. -->

<!-- Link your sprint planner task here.-->

[Planner task](link-to-planner-task)

## Before opening this, I checked

- [ ] My PR only contains one logical change that is easy to review <!-- Split unrelated changes into separate PRs - smaller PRs get reviewed faster -->
- [ ] Existing code doesn't already do this <!-- Search the repo first - can you extend or reuse instead of duplicating? -->
- [ ] No open PRs touch the same files or fields <!-- If they do, list them below and reach out to the author to coordinate -->
  - Overlaps: <!-- list PR links, or write N/A -->
- [ ] Changes are consistent with the formulation document and other agreed conventions <!-- formulation doc = the math model; also check naming conventions, directory structure, and other team contracts. If something's inconsistent, update it here or explain why below -->

## Running it

<!-- Everything needed from scratch: install, env vars, then the commands
     to run the code and the tests. Follow your own steps once before posting. -->

```zsh
# e.g.
pip install -r requirements.txt
python -m src.model
```

- [ ] Tests pass from the repository root and from another directory
- [ ] Nothing committed that should not be: no .env, credentials, generated or solver output files

## Testing

What do the tests verify, not just that they pass? What is not covered?

<!-- Good: "an out-of-limit blend returns Infeasible"
     Weak: "the function returns a float" -->

## Note for the reviewer

<!-- What to look at hardest - where you are least confident? -->
