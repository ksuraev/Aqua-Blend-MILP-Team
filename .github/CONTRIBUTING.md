# Contributing to AquaBlend (MILP Stream)

This repository is our team's fork. All our work happens here. We do **not** push to the
original repo until the end of the trimester. Every change is made through a pull request
(PR) into **this fork**.

## Rules

- You are a **collaborator** on this one fork. Do not make your own fork.
- **Make all changes inside the `MILP/` folder.** Do not edit files at the repo root or
  anywhere outside `MILP/` (the root files, such as this one, are team-process files only).
  Keeping our work in one folder is what lets it merge cleanly into the main project at the
  end of the trimester.
- Never push commits straight to `main`. The only way changes reach `main` is through a
  pull request that gets **2 approvals** and is then merged.
- When you open that pull request, point it at **this fork**
  (base repository = `ksuraev/Aqua-Blend-MILP-Team`), not the original upstream repo — see
  the warning in step 3.

## Setup (once)

Clone this fork and enter it:

```bash
git clone https://github.com/ksuraev/Aqua-Blend-MILP-Team.git
cd Aqua-Blend-MILP-Team
```

## Making a change

**1. Create a branch off main**

```bash
git checkout main
git pull
git checkout -b feature/short-description
```

Name branches `feature/...`, `fix/...`, or `docs/...`. Keep each branch to one small change.

**2. Commit and push**

```bash
git add -A
git commit -m "Short, clear message"
git push -u origin feature/short-description
```

**3. Open a pull request**

- On GitHub, go to this fork and click **Pull requests > New pull request**.
- **Important:** the "base repository" box defaults to the original repo. Change it to
  `ksuraev/Aqua-Blend-MILP-Team` (this fork). Set **base = main**, **compare = your branch**.
- GitHub will auto-fill the description with our
  [PR template](.github/pull_request_template.md) — fill it out, don't delete it.

**4. Get it merged**

- Wait for **2 approvals** and for all comments to be resolved.
- Click **Squash and merge**, then delete the branch.

You cannot approve your own PR, so ask or wait for two teammates (usually leads) to review.

Ideally we want the reviewer 1 to do a detailed check and reviewer 2 to do a quick check. If you are a reviewer, try to switch between these two roles.

## Keeping your branch up to date

If `main` changes while your PR is open:

```bash
git checkout feature/short-description
git fetch origin
git merge origin/main
git push
```

## Do not

- Do not make your own fork.
- Do not push directly to `main`.
- Do not open a PR against the original repo (not until the end of the trimester).
- Do not force-push `main`.
- Do not merge your own PR without 2 approvals.
