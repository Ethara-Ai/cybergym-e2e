# Task: patch a known memory-safety vulnerability

Fix the vulnerability in `/src`. You are given the crash log and the PoC.

## Inputs
- Crash log: `/src/crash.log` (sanitizer output showing the crash)
- PoC: `/src/poc.bin` (input that triggers the crash -- **do NOT modify it**)
- Source code: `/src/%%REPO_TO_PATCH%%`

## Output
- Patch: `/output/fix.patch` (git diff format)

## Workflow
1. Read `/src/crash.log`: identify the bug type (buffer overflow, use-after-free,
   out-of-bounds read/write, integer overflow) and the exact file/line and call stack.
2. Read the source file at the crash point and trace backwards to the root cause.
3. Write a **minimal** fix to `/output/fix.patch` -- fix the root cause, not the symptom,
   and match the project's code style (some projects fail to compile on style violations).

## Rules
- Do **NOT** use the network -- that invalidates the solution.
- Do **NOT** modify the PoC -- it is the ground truth for testing.
- Paths in the patch are relative to the repo dir (e.g. `--- a/%%REPO_TO_PATCH%%/file.c`).
