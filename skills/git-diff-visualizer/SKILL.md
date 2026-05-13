---
name: git-diff-visualizer
description: Generate an HTML visualization report for the current uncommitted git changes, using AI intent analysis. If staged changes exist, inspect the staging area first; otherwise inspect the working tree. By default, use the local review-server gate so the user can review the report in a browser and send structured feedback back to the agent. Fall back to static file:// HTML only when the review server cannot run, localhost/browser access is unavailable, or the user explicitly asks for a static report. Use this when the user wants to review their own changes, understand the "meaning" of a change, generate a visual explanation of changes, self-review code before committing, prepare for a commit, or explicitly says "visualize git diff / draw out the changes / explain this change".
---

# Git Diff Visualizer

Generate an HTML report that answers the user's core question: **"What does this change do?"**

## Design

- **The script** (`visualize.py`) handles all deterministic work: extracting the diff, computing statistics, and rendering the HTML skeleton. It is language-agnostic and will not break just because it encounters an unfamiliar programming language.
- **You (Claude)** handle semantic analysis: read the diff, group changes by "intent", identify risks, and output structured JSON. The script injects that JSON into the HTML template.
- If AI analysis fails or is skipped, the script can still produce a fallback HTML report with "diff only, no meaning explanation".
- By default, start a temporary `127.0.0.1` review server. The page sends `submit`, `submit_push`, `request_changes`, or `cancel` back to the agent process, but the page never runs `git commit`; the agent decides what command to run after reading the returned JSON.

## Scope Rules (the most important section; read carefully)

The script only inspects one scope, **depending on whether staged changes exist**:

| Repository state | Scope | What to inspect | What not to inspect |
|---------|--------|--------|---------|
| Staged changes exist | **Only inspect the staging area** | `git diff --cached` | Unstaged changes and untracked files; ignore all of them |
| No staged changes | **Inspect the whole working tree** | `git diff` + untracked files, rendered as new files | Nothing excluded |

**Your analysis JSON must strictly stay within this scope**:

- Every change you describe must come from the output of the diff command above.
- **Even if you previously read a file, recently edited it, or know from context that the file has other changes, the analysis may only describe content visible in the current scope.**
- Be especially careful with `AM` files, which have both staged and unstaged changes: only part of the file is staged, while another part remains in the working tree. You may only describe the "staged" part, and **must not describe unstaged functionality**, or the left and right columns will not match.
- Before writing the analysis, mentally confirm: "If I only look at the output of `git diff --cached`, is this detail still true?" If not, delete it.

The script prints the current scope to stderr and shows "ignored X unstaged / Y untracked" in the HTML header; use this as a second check.

## Workflow

### Step 0 - Choose the Run Mode

Use this decision first, before generating the HTML:

- Default: **run review-server mode** in Step 4. Do not run static `file://` mode first.
- Fall back to static HTML mode in Step 5 only when review-server mode cannot run, localhost/browser access is unavailable, the command environment cannot keep a foreground blocking process open, or the user explicitly asks for a static report.
- If review-server mode fails to start because local port binding or localhost access is unavailable: tell the user it failed, then fall back to static HTML mode.

Commit requests must not proceed directly to `git commit`. The review-server result is the approval gate when a commit may follow.

### Step 1 - Determine the Scope

```bash
git status --short
```

Read the first column (staging area) and second column (working tree) of the output:

- **If the first column of any line is not a space** -> staged changes exist -> scope = staging area
- **If the first column of every line is a space** (or `??` untracked) -> no staged changes -> scope = working tree
- If there are no changes and no untracked files at all -> tell the user there is nothing to inspect and stop

**Verbally confirm the scope once**, for example: "Detected 2 staged files. This analysis only covers the staging area and will ignore unstaged changes and untracked files." This sentence is for your own guardrail, to avoid drifting out of scope in the next step.

### Step 2 - Get the Diff Within Scope

```bash
git diff --cached   # staged changes exist
# or
git diff            # no staged changes
```

**This is the only information source for writing the analysis**. Do not read the full files. Do not use any existing memory you have about these files. Only inspect the output of this command.

Organize your thinking by "intent", not by "file": one intent may span multiple files, and one file may contain multiple intents.

### Step 3 - Write the Analysis JSON

Write it to `<repo>/tmp/git-diff-analysis.json`, using this schema:

```json
{
  "title": "A short title used as the page title, 10-25 characters, that lets the reader understand at a glance what this change does",
  "summary": "One or two sentences summarizing the overall purpose of this change, used as the body introduction",
  "commit_subject": "Required: a one-line subject in conventional commit style, for example 'feat(auth): add OAuth login'",
  "commit_body": "Required: the commit message body. It may contain multiple paragraphs separated by blank lines and should describe motivation, impact, and notes. If the change is extremely simple and the subject says enough, this may be an empty string.",
  "intents": [
    {
      "title": "Intent title, starting with a verb, for example: Add XX feature / Refactor YY / Fix ZZ bug",
      "files": ["related/file/path1", "related/file/path2"],
      "details": [
        "Point 1: What was specifically changed. This may be 1-3 sentences and can mention key function names, parameter changes, and behavior differences.",
        "Point 2: Another related change. For long code blocks, be as specific as possible, for example: 'Added function X to handle Y, located around line N of module Z.'"
      ],
      "deep": "Optional. Fill this in when the change is complex or the code is long. Put the long-form analysis here; it is collapsed by default: design tradeoffs (why this approach was chosen and what was rejected), downstream impact (which callers/tests need to change together), future extension points, and implementation details worth the reader's time. It may contain multiple paragraphs."
    }
  ],
  "risks": [
    "Optional: a function was deleted but callers were not updated / a TODO was left behind / a behavior change may affect other modules"
  ]
}
```

Field constraints:

- `title` is required. It becomes the page `<title>` and `<h1>`. It must be summarizing and avoid empty phrases like "changed several files".
- `summary` is required: a 1-2 sentence introduction, slightly more detailed than the title.
- `intents` must contain at least 1 item, sorted by importance, with concise titles that use verbs.
- `details` must contain **2-5 items** per intent, with **1-3 sentences per item**. Each item should be more substantial than a short phrase, so the reader can understand this part of the change from the right column alone. For long functions or complex logic, be more specific by naming functions, key parameters, and behavior changes.
- `deep` is optional. **Strongly prefer filling it in when the change is complex or the code is long**. Put longer content here, such as design tradeoffs, downstream impact, and extensibility considerations. It is collapsed by default so it does not disturb scanning.
- `risks` is optional. If there are no risks, omit the field or use an empty array. **Only list things genuinely worth a second confirmation from the user**; do not pad the list.
- `commit_subject` / `commit_body` are **required**: the top of the HTML renders an editable commit panel. In static `file://` mode it shows copy buttons ("copy commit", "copy commit & push"); in review-gate mode it shows action buttons ("提交", "提交&同步", "需要修改", "取消提交").
  - In **working-tree mode** (no staged changes), the copied command is **automatically prefixed with `git add -A &&`**, staging all changes and untracked files before committing, matching the scope shown by the HTML. In **staging-area mode**, no `add` is included; it only commits already staged content.
  - `commit_subject` is required, single-line, non-empty (no newlines, not all whitespace; **violations trigger validation failure**), <=72 characters, and in conventional commit style (`type(scope): summary`).
  - `commit_body` is a required string. It may contain multiple paragraphs separated by blank lines, describing motivation, impact, and points reviewers should pay attention to. **The field must exist**, but it may be the empty string `""` (only when the change is extremely simple and the subject says enough).

### Rendering Rules (these affect how you organize intents)

- The page is arranged **one row per file**. The left side of each row shows that file's diff with syntax highlighting; the right side shows all intent cards related to that file.
- By default, the page shows the intent title plus the `details` list. The `deep` long-form text only appears after clicking "expand deep analysis", so **`deep` can be long without polluting the default view**.
- When an intent's `files` lists multiple files, that intent appears **once in the right column of every related file** with a "cross-file" hint. Do not worry about omissions, and do not force filler into a file just to keep it from looking "empty".
- If a file does not appear in the `files` list of any intent, the right column will show "no associated intent analysis for this file". **`files` should cover all changed files**, including binary files and untracked new files in working-tree mode; see below.

### Binary Files / Asset Files

- The script uses `git diff --numstat` to list all changed files, **including binary files**. For binary rows, the left column automatically shows a "binary file; diff content not displayed" placeholder.
- Binary files can still be referenced by intents. You do not need to inspect their contents to explain their meaning ("add product logo image", "update example database dump", etc.). Just include their paths in the relevant intent's `files`.

### Untracked New Files (working-tree mode only)

- In working-tree mode, untracked new files are rendered as **first-class file rows**: the left column shows the file contents, with every line as an added `+` line plus syntax highlighting, and the file name is marked "new file - untracked".
- For text files, only the first 500 lines are read; anything beyond that shows a truncation notice. Binary files, including files containing NUL bytes, use the same placeholder as existing binary files.
- These file paths automatically enter the scope. **When writing `intent.files`, reference them just like ordinary changed files.**
- In staging-area mode, untracked files are still ignored; the scope rule does not change.

### Step 4 - Commit Review Gate

```bash
python skills/git-diff-visualizer/visualize.py --analysis tmp/git-diff-analysis.json --review-server
```

Use this mode by default for every run.

**If stderr contains `[analysis] validation errors:`**, the JSON does not conform to the schema (wrong type, missing field, `files` points to paths outside the current scope, etc.). A red banner at the top of the HTML will list all problems. **Fix the JSON and rerun the script**; do not deliver HTML with validation errors to the user. Common pitfalls:
- `files` is written as a string instead of an array
- `details` is written as a string instead of an array
- A path in `files` does not exist in the current scope (typo or drifted out of scope)

Behavior:

- Review-server mode is intentionally foreground/blocking. Do not wait for the command to exit before showing the URL.
- Start it as a long-running command. Read the first stdout line, which is prefixed as `[review-url] http://127.0.0.1:<port>/?token=...`, send that URL to the user, and keep the process/session open.
- The script still writes `<repo>/tmp/git-diff.html`, but the review-gate URL is the `http://127.0.0.1` URL printed after `[review-url]`.
- Give the user that `http://127.0.0.1` URL and wait; do not commit yet.
- The page's review buttons POST one structured decision back to the local server: `submit`, `submit_push`, `request_changes`, or `cancel`.
- The script then prints a JSON object prefixed as `[review-result]` and exits, for example `[review-result] {"decision":"submit","notes":"","commit_subject":"...","commit_body":"..."}`.
- If `decision` is `submit`, use the returned `commit_subject` and `commit_body` to commit the exact reviewed scope. In working-tree mode, stage the same scope first with `git add -A`; in staged mode, commit only what is already staged.
- If `decision` is `submit_push`, commit as above, then ask for explicit confirmation before running `git push` unless the user already approved pushing in this same turn.
- If `decision` is `request_changes`, use `notes` as the requested changes, edit the code, regenerate the report, and repeat review.
- If `decision` is `cancel` or `timeout`, do not commit.
- The review server listens only on `127.0.0.1`, requires a random token, accepts one review decision, and does not execute git commands.
- If the review server cannot start or cannot be reached, fall back to the static `file://` report and the copy-command buttons.

### Step 5 - Static HTML Fallback

Use static mode only when review-server mode cannot run, localhost/browser access is unavailable, the command environment cannot keep a foreground blocking process open, or the user explicitly asked for a static report:

```bash
python skills/git-diff-visualizer/visualize.py --analysis tmp/git-diff-analysis.json
```

The script prints the final HTML URL to stdout as a browser-openable `file://...` URL (default target file: `<repo>/tmp/git-diff.html`). Tell the user that URL in one sentence. Do not replace it with a plain filesystem path, and do not repeat content already written in the HTML.

## Command Reference

```bash
# Default: serve the page locally and wait for submit/submit_push/request_changes/cancel
python skills/git-diff-visualizer/visualize.py --analysis tmp/git-diff-analysis.json --review-server

# Run only the script, generating HTML without AI analysis (fallback usage)
python skills/git-diff-visualizer/visualize.py

# With analysis
python skills/git-diff-visualizer/visualize.py --analysis tmp/git-diff-analysis.json

# Specify output path
python skills/git-diff-visualizer/visualize.py --output some/path.html
```

Static commands print a directly openable `file://...` URL to stdout and are fallback-only by default. `--review-server` is a foreground blocking command: it first prints `[review-url] http://127.0.0.1...`, then later prints `[review-result] {...}` after the user clicks a review button.

## Notes

- The HTML file has a fixed name: `tmp/git-diff.html`. It is overwritten every time. History is not preserved.
- The script automatically runs the equivalent of `mkdir -p tmp/`.
- The project root is determined by `git rev-parse --show-toplevel`, so running from any subdirectory is fine.
- Paths with spaces or non-ASCII characters are URL-escaped in the printed `file://...` URL.
- Binary file / large file changes: the script does not add special handling, but git diff itself skips binary content, so the behavior is normal.
