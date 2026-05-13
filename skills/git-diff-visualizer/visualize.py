#!/usr/bin/env python3
"""Generate an HTML visualization of uncommitted git changes.

Deterministic parts (diff extraction, stats, HTML rendering) are handled here.
The "intent analysis" section is filled in from a JSON file produced by Claude.
"""
import argparse
import html
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


def run_git(args, check=True):
    result = subprocess.run(
        ["git"] + args, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        if check:
            print(f"git {' '.join(args)} failed: {result.stderr}", file=sys.stderr)
            sys.exit(1)
        return None
    return result.stdout


def has_staged():
    return bool(run_git(["diff", "--cached", "--name-only"]).strip())


def get_diff(staged):
    return run_git(["diff", "--cached"] if staged else ["diff"])


def get_stats(staged):
    args = ["diff", "--cached", "--numstat"] if staged else ["diff", "--numstat"]
    files = []
    total_add = total_del = 0
    for line in run_git(args).strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, dele, path = parts[0], parts[1], parts[2]
        binary = (add == "-" and dele == "-")
        a = 0 if add == "-" else int(add)
        d = 0 if dele == "-" else int(dele)
        files.append({"path": path, "added": a, "deleted": d, "binary": binary})
        total_add += a
        total_del += d
    return {"files": files, "added": total_add, "deleted": total_del}


def get_untracked(staged):
    if staged:
        return []
    out = run_git(["ls-files", "--others", "--exclude-standard"])
    return [f for f in out.strip().split("\n") if f]


MAX_UNTRACKED_LINES = 500


def read_untracked_file(path):
    """Read an untracked file with line cap. Returns dict with binary detection."""
    p = Path(path)
    if not p.is_file():
        return {"binary": False, "lines": [], "truncated": False, "total_lines": 0,
                "error": "not a regular file"}
    try:
        with open(p, "rb") as f:
            head = f.read(8192)
    except OSError as e:
        return {"binary": False, "lines": [], "truncated": False, "total_lines": 0,
                "error": str(e)}
    if b"\x00" in head:
        return {"binary": True, "lines": [], "truncated": False, "total_lines": 0}

    lines = []
    truncated = False
    total = 0
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                total += 1
                if total <= MAX_UNTRACKED_LINES:
                    lines.append(line.rstrip("\n").rstrip("\r"))
                else:
                    truncated = True
    except OSError as e:
        return {"binary": False, "lines": lines, "truncated": truncated, "total_lines": total,
                "error": str(e)}
    return {"binary": False, "lines": lines, "truncated": truncated, "total_lines": total}


def get_untracked_with_content(staged):
    """Workspace mode: read untracked file contents (with line cap). Empty in staged mode."""
    if staged:
        return []
    return [{**read_untracked_file(p), "path": p} for p in get_untracked(staged)]


def untracked_to_row(u):
    """Convert an untracked file record to a row_data entry compatible with render_file_rows."""
    if u.get("binary"):
        return {
            "path": u["path"],
            "binary": True,
            "added": 0,
            "deleted": 0,
            "diff_html": None,
            "untracked": True,
        }
    lines = u.get("lines", [])
    total = u.get("total_lines", len(lines))
    truncated = u.get("truncated", False)
    error = u.get("error")
    lang = detect_language(u["path"])

    if error:
        header = f"@@ 新文件 · 读取失败：{error} @@"
    elif truncated:
        header = f"@@ 新文件 · 共 {total} 行 · 仅显示前 {MAX_UNTRACKED_LINES} 行 @@"
    else:
        header = f"@@ 新文件 · 共 {total} 行 @@"
    parts = [f'<div class="hunk">{html.escape(header)}</div>']
    for ln in lines:
        parts.append(_render_code_line("+", ln, lang, "add"))
    if truncated:
        remaining = total - len(lines)
        parts.append(
            f'<div class="hunk">{html.escape(f"@@ …… 还有 {remaining} 行被截断 @@")}</div>'
        )
    return {
        "path": u["path"],
        "binary": False,
        "added": len(lines),
        "deleted": 0,
        "diff_html": "\n".join(parts),
        "untracked": True,
    }


def get_excluded_counts(staged):
    """When in staged mode, count what we're ignoring so the user can verify scope."""
    if not staged:
        return None
    unstaged_count = 0
    out = run_git(["diff", "--numstat"])
    for line in out.strip().split("\n"):
        if line.strip():
            unstaged_count += 1
    untracked_out = run_git(["ls-files", "--others", "--exclude-standard"]).strip()
    untracked_count = sum(1 for u in untracked_out.split("\n") if u)
    return {"unstaged_files": unstaged_count, "untracked_files": untracked_count}


def get_meta():
    branch = run_git(["branch", "--show-current"]).strip() or "(detached)"
    head_raw = run_git(["rev-parse", "--short", "HEAD"], check=False)
    head = head_raw.strip() if head_raw else "(no commits)"
    return {
        "branch": branch,
        "head": head,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_repo_root():
    return Path(run_git(["rev-parse", "--show-toplevel"]).strip())


EXT_LANG = {
    ".py": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".md": "markdown", ".markdown": "markdown",
    ".json": "json",
    ".html": "xml", ".htm": "xml", ".xml": "xml", ".svg": "xml",
    ".css": "css", ".scss": "scss", ".less": "less",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".yml": "yaml", ".yaml": "yaml",
    ".toml": "ini",
    ".ini": "ini", ".cfg": "ini",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".sql": "sql",
    ".swift": "swift",
    ".lua": "lua",
}


def detect_language(path):
    p = Path(path)
    ext = p.suffix.lower()
    if ext in EXT_LANG:
        return EXT_LANG[ext]
    name = p.name.lower()
    if name == "dockerfile" or name.endswith(".dockerfile"):
        return "dockerfile"
    if name == "makefile":
        return "makefile"
    return "plaintext"


def _render_code_line(marker_char, content, lang, cls):
    """Render one diff line with marker + highlightable code."""
    marker_html = html.escape(marker_char if marker_char else " ")
    content_html = html.escape(content)
    return (
        f'<div class="line {cls}"><span class="marker">{marker_html}</span>'
        f'<code class="language-{lang}">{content_html}</code></div>'
    )


def parse_diff_by_file(diff_text):
    """Split a unified diff into per-file HTML blocks. Returns [(path, html), ...] in diff order."""
    if not diff_text.strip():
        return []

    files = []
    current_path = None
    current_lang = "plaintext"
    current_lines = []

    def flush():
        if current_path is not None:
            files.append((current_path, "\n".join(current_lines)))

    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            flush()
            m = re.search(r" b/(.+)$", line)
            current_path = m.group(1) if m else "unknown"
            current_lang = detect_language(current_path)
            current_lines = []
        elif current_path is None:
            continue
        elif line.startswith("@@"):
            current_lines.append(f'<div class="hunk">{html.escape(line)}</div>')
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("index ") or line.startswith("new file") or line.startswith(
            "deleted file"
        ) or line.startswith("similarity") or line.startswith("rename") or line.startswith(
            "Binary files"
        ):
            continue
        elif line.startswith("+"):
            current_lines.append(_render_code_line("+", line[1:], current_lang, "add"))
        elif line.startswith("-"):
            current_lines.append(_render_code_line("-", line[1:], current_lang, "del"))
        else:
            content = line[1:] if line.startswith(" ") else line
            current_lines.append(_render_code_line(" ", content, current_lang, "ctx"))

    flush()
    return files


def validate_analysis(analysis, scope_paths):
    """Return list of human-readable error strings; empty = valid."""
    errors = []
    if not isinstance(analysis, dict):
        return ["analysis 顶层必须是 JSON 对象"]

    required = {"title": str, "summary": str, "intents": list,
                "commit_subject": str, "commit_body": str}
    for field, exp in required.items():
        if field not in analysis:
            errors.append(f"缺少必填字段 `{field}`")
        elif not isinstance(analysis[field], exp):
            actual = type(analysis[field]).__name__
            errors.append(f"`{field}` 类型应为 {exp.__name__}，实际为 {actual}")

    intents = analysis.get("intents")
    if isinstance(intents, list):
        if not intents:
            errors.append("`intents` 至少需要 1 项")
        for i, intent in enumerate(intents):
            ctx = f"intents[{i}]"
            if not isinstance(intent, dict):
                errors.append(f"{ctx} 必须是对象")
                continue
            if not isinstance(intent.get("title"), str):
                errors.append(f"{ctx}.title 缺失或不是字符串")
            files = intent.get("files")
            if files is None:
                errors.append(f"{ctx}.files 缺失")
            elif not isinstance(files, list):
                errors.append(
                    f"{ctx}.files 必须是数组（写成字符串会被当作字符列表，导致跨文件标签乱码）"
                )
            else:
                if not files:
                    errors.append(f"{ctx}.files 不能为空数组")
                for j, f in enumerate(files):
                    if not isinstance(f, str):
                        errors.append(f"{ctx}.files[{j}] 必须是字符串")
                    elif f not in scope_paths:
                        errors.append(
                            f"{ctx}.files[{j}] 路径 `{f}` 不在当前 diff 作用域内，"
                            "这个 intent 不会显示在任何文件右栏"
                        )
            if "details" in intent:
                if not isinstance(intent["details"], list):
                    errors.append(
                        f"{ctx}.details 必须是数组（写成字符串会被逐字符渲染为列表项）"
                    )
                else:
                    for j, d in enumerate(intent["details"]):
                        if not isinstance(d, str):
                            errors.append(f"{ctx}.details[{j}] 必须是字符串")
            if "deep" in intent and not isinstance(intent["deep"], str):
                errors.append(f"{ctx}.deep 必须是字符串")

    if "risks" in analysis:
        if not isinstance(analysis["risks"], list):
            errors.append("`risks` 必须是数组（写成字符串会被逐字符渲染）")
        else:
            for i, r in enumerate(analysis["risks"]):
                if not isinstance(r, str):
                    errors.append(f"risks[{i}] 必须是字符串")

    if isinstance(analysis.get("commit_subject"), str):
        if "\n" in analysis["commit_subject"]:
            errors.append("`commit_subject` 不能包含换行（subject 应保持单行）")
        if not analysis["commit_subject"].strip():
            errors.append("`commit_subject` 不能为空字符串")

    return errors


def render_validation_errors(errors):
    if not errors:
        return ""
    items = "\n".join(f"<li>{html.escape(e)}</li>" for e in errors)
    return (
        f'<div class="validation-error">'
        f'<strong>⚠ analysis JSON 校验失败（{len(errors)} 项）</strong>'
        f'<ul>{items}</ul>'
        f'<p>下方仍按现有字段渲染——但内容可能错位、错乱或缺失。修正 JSON 后重新跑脚本。</p>'
        f'</div>'
    )


def render_intent_card(intent):
    title = intent.get("title", "未命名意图")
    parts = [f'<div class="intent-card"><h4>{html.escape(title)}</h4>']
    if intent.get("details"):
        parts.append("<ul>")
        for d in intent["details"]:
            parts.append(f"<li>{html.escape(d)}</li>")
        parts.append("</ul>")
    files = intent.get("files", [])
    if len(files) > 1:
        parts.append(
            f'<div class="cross-file">跨文件：{html.escape(", ".join(files))}</div>'
        )
    if intent.get("deep"):
        parts.append(
            '<details class="deep"><summary>展开深入分析</summary>'
            f'<div class="deep-content">{html.escape(intent["deep"])}</div></details>'
        )
    parts.append("</div>")
    return "\n".join(parts)


def render_file_rows(rows_data, analysis):
    if not rows_data:
        return '<p class="placeholder">没有改动</p>'

    intents = (analysis or {}).get("intents", [])
    rows = []
    for row in rows_data:
        path = row["path"]
        relevant = [i for i in intents if path in i.get("files", [])]
        if relevant:
            analysis_html = "\n".join(render_intent_card(i) for i in relevant)
        else:
            analysis_html = '<p class="placeholder">该文件无关联意图分析</p>'

        if row["binary"]:
            diff_html = '<div class="diff-placeholder">二进制文件，diff 内容不展示</div>'
        elif row["diff_html"]:
            diff_html = f'<div class="diff-body">{row["diff_html"]}</div>'
        else:
            diff_html = '<div class="diff-placeholder">无 diff 内容</div>'

        stats_html = ""
        if not row["binary"]:
            stats_html = (
                f'<span class="file-stats">'
                f'<span class="add-count">+{row["added"]}</span> '
                f'<span class="del-count">−{row["deleted"]}</span></span>'
            )
        else:
            stats_html = '<span class="file-stats">binary</span>'

        badge_html = ""
        if row.get("untracked"):
            badge_html = '<span class="badge new-file">新文件 · 未跟踪</span>'

        rows.append(
            f'<div class="file-row">'
            f'<div class="file-name">'
            f'<span class="file-path">{html.escape(path)}</span>'
            f'{badge_html}'
            f'{stats_html}'
            f'</div>'
            f'<div class="file-content">'
            f'<div class="diff-col">{diff_html}</div>'
            f'<div class="analysis-col">{analysis_html}</div>'
            f'</div></div>'
        )
    return "\n".join(rows)


def render_summary(analysis):
    if not analysis:
        return (
            '<p class="placeholder">AI 意图分析未生成。'
            '使用 <code>--analysis path/to/analysis.json</code> 注入分析结果。</p>'
        )
    summary = analysis.get("summary", "")
    if not summary:
        return ""
    return f'<div class="summary"><p>{html.escape(summary)}</p></div>'


def render_commit_box(analysis, staged):
    """Render the commit box: editable subject/body + clipboard buttons."""
    subject = (analysis or {}).get("commit_subject", "") if analysis else ""
    body = (analysis or {}).get("commit_body", "") if analysis else ""
    placeholder_subject = "feat: ..." if not subject else ""
    placeholder_body = "可选：详细说明（保留空行分段）" if not body else ""
    scope_hint = (
        '（工作区模式：命令会自动前置 <code>git add -A</code>）'
        if not staged else
        '（暂存模式：仅提交已暂存内容）'
    )
    return (
        f'<section class="commit-box" data-staged="{"true" if staged else "false"}">'
        '<h2>提交 '
        f'<span class="commit-scope-hint">{scope_hint}</span>'
        '</h2>'
        '<div class="commit-fields">'
        '<label class="commit-label">Subject'
        f'<input type="text" id="commit-subject" value="{html.escape(subject)}"'
        f' placeholder="{html.escape(placeholder_subject)}" spellcheck="false"></label>'
        '<label class="commit-label">Body'
        f'<textarea id="commit-body" rows="4"'
        f' placeholder="{html.escape(placeholder_body)}" spellcheck="false">{html.escape(body)}</textarea>'
        '</label>'
        '</div>'
        '<div class="commit-actions">'
        '<button type="button" id="btn-commit">复制 commit 命令</button>'
        '<button type="button" id="btn-commit-push">复制 commit &amp; push 命令</button>'
        '<span id="copy-feedback" class="copy-feedback" aria-live="polite"></span>'
        '</div>'
        '<details class="commit-preview"><summary>预览命令</summary>'
        '<pre id="commit-preview-text"></pre></details>'
        '</section>'
    )


def render_risks(analysis):
    risks = (analysis or {}).get("risks") or []
    if not risks:
        return ""
    items = "\n".join(f"<li>{html.escape(r)}</li>" for r in risks)
    return f'<section><h2>值得注意的地方</h2><div class="risks"><ul>{items}</ul></div></section>'


def page_title(analysis):
    if analysis:
        if analysis.get("title"):
            return analysis["title"]
        if analysis.get("summary"):
            s = analysis["summary"]
            return s if len(s) <= 40 else s[:40] + "…"
    return "Git 改动可视化"


CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 1500px; margin: 2em auto; padding: 0 1em; color: #24292e; }
header { border-bottom: 1px solid #e1e4e8; padding-bottom: 1em; margin-bottom: 1em; }
h1 { margin: 0 0 0.3em 0; font-size: 1.6em; }
h2 { border-bottom: 1px solid #e1e4e8; padding-bottom: 0.3em; margin-top: 2em; font-size: 1.15em; }
.meta { color: #586069; font-size: 0.88em; }
.stats { display: flex; gap: 1.5em; margin: 0.6em 0 0 0; font-size: 0.92em; }
.add-count { color: #28a745; font-weight: 600; }
.del-count { color: #d73a49; font-weight: 600; }
.summary { background: #f6f8fa; padding: 0.8em 1.2em; border-radius: 6px;
           border-left: 4px solid #0366d6; margin-bottom: 1.5em; }
.summary p { margin: 0; }
.scope-note { background: #fff5e6; padding: 0.6em 1em; border-radius: 6px;
              border-left: 4px solid #f9a826; font-size: 0.9em; margin: 0.8em 0; }
.validation-error { background: #ffebe9; padding: 0.8em 1.2em; border-radius: 6px;
                    border-left: 4px solid #d73a49; margin: 0.8em 0; font-size: 0.9em; }
.validation-error strong { color: #d73a49; }
.validation-error ul { margin: 0.4em 0; padding-left: 1.4em; }
.validation-error p { margin: 0.4em 0 0 0; color: #586069; font-size: 0.85em; }
.placeholder { color: #586069; font-style: italic; }

.file-row { border: 1px solid #e1e4e8; border-radius: 6px; margin: 1em 0; overflow: hidden; }
.file-name { background: #f6f8fa; padding: 0.5em 1em; font-family: ui-monospace, monospace;
             font-size: 0.9em; border-bottom: 1px solid #e1e4e8;
             display: flex; align-items: center; gap: 0.6em; }
.file-name .file-path { font-weight: 600; }
.file-content { display: grid; grid-template-columns: 1fr 1fr; gap: 0; align-items: start; }
.diff-col { border-right: 1px solid #e1e4e8; overflow-x: auto; overflow-y: hidden;
            background: #fff; min-width: 0; position: relative; }
.diff-col.expanded { overflow-y: visible; max-height: none !important; }
.diff-col.clipped:not(.expanded)::after {
  content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 36px;
  background: linear-gradient(rgba(255,255,255,0), rgba(255,255,255,0.95));
  pointer-events: none;
}
.analysis-col { padding: 0.8em 1em; background: #fafbfc; min-width: 0; }
.diff-toggle { position: absolute; left: 50%; bottom: 0.5em;
               transform: translateX(-50%); z-index: 2;
               padding: 0.25em 0.9em; border: 1px solid #d0d7de;
               background: #fff; color: #0366d6; cursor: pointer; border-radius: 999px;
               font-size: 0.82em; font-family: inherit;
               box-shadow: 0 1px 4px rgba(0,0,0,0.12); }
.diff-toggle:hover { background: #f6f8fa; border-color: #0366d6; }
.diff-col.expanded { padding-bottom: 2.6em; }
@media (max-width: 1000px) {
  .file-content { grid-template-columns: 1fr; }
  .diff-col { border-right: 0; border-bottom: 1px solid #e1e4e8; }
}

.diff-body { font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 0.8em; }
.line { padding: 0; min-height: 1.4em; line-height: 1.4em; display: flex; align-items: baseline; }
.line.add { background: #e6ffec; }
.line.del { background: #ffebe9; }
.line.ctx { color: #24292e; }
.line .marker { display: inline-block; width: 1.6em; padding-left: 0.6em; flex-shrink: 0;
                color: #999; user-select: none; }
.line code { background: transparent !important; padding: 0 !important; color: inherit;
             font: inherit !important; white-space: pre; flex: 1 1 auto; min-width: 0; }
.line code .hljs-comment, .line code .hljs-quote { color: #6a737d; }
.hunk { background: #ddf4ff; color: #0550ae; padding: 0.25em 1em;
        font-family: ui-monospace, monospace; font-size: 0.8em; }
.diff-placeholder { padding: 1em; color: #586069; font-style: italic; font-size: 0.9em; }

.file-name .file-stats { font-weight: 400; font-size: 0.85em; margin-left: 0.6em; }
.badge { display: inline-block; padding: 0.1em 0.5em; border-radius: 3px;
         font-size: 0.72em; font-weight: 600; margin-left: 0.4em;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.badge.new-file { background: #dafbe1; color: #1a7f37; border: 1px solid #b6e3c1; }
.intent-card { background: #fff; border: 1px solid #e1e4e8; border-radius: 4px;
               padding: 0.5em 0.8em; margin-bottom: 0.6em; }
.intent-card:last-child { margin-bottom: 0; }
.intent-card h4 { margin: 0 0 0.3em 0; font-size: 0.95em; color: #0366d6; }
.intent-card ul { margin: 0; padding-left: 1.2em; font-size: 0.88em; line-height: 1.55; }
.intent-card ul li { margin-bottom: 0.2em; }
.intent-card .cross-file { margin-top: 0.4em; font-size: 0.8em; color: #586069;
                            font-family: ui-monospace, monospace; }
.intent-card details.deep { margin-top: 0.5em; border-top: 1px dashed #e1e4e8; padding-top: 0.4em; }
.intent-card details.deep summary { cursor: pointer; color: #0366d6; font-size: 0.85em;
                                     list-style: none; padding: 0.2em 0; }
.intent-card details.deep summary::before { content: "▸ "; }
.intent-card details.deep[open] summary::before { content: "▾ "; }
.intent-card details.deep .deep-content { margin-top: 0.4em; font-size: 0.88em;
                                            line-height: 1.6; color: #444;
                                            white-space: pre-wrap; }

.risks { background: #fffbdd; padding: 0.8em 1.2em; border-radius: 6px;
         border-left: 4px solid #f9c513; }
.risks ul { margin: 0; padding-left: 1.2em; }

.commit-box { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px;
              padding: 1em 1.2em; margin: 1.5em 0; }
.commit-box h2 { margin-top: 0; border-bottom: none; padding-bottom: 0; }
.commit-scope-hint { font-size: 0.7em; color: #586069; font-weight: 400; margin-left: 0.4em; }
.commit-scope-hint code { background: #eaeef2; padding: 0.05em 0.35em; }
.commit-fields { display: flex; flex-direction: column; gap: 0.6em; margin-bottom: 0.8em; }
.commit-label { display: flex; flex-direction: column; font-size: 0.85em; color: #586069;
                font-weight: 600; gap: 0.3em; }
.commit-label input, .commit-label textarea {
  font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 0.9em;
  padding: 0.5em 0.7em; border: 1px solid #d0d7de; border-radius: 4px;
  background: #fff; color: #24292e; resize: vertical;
}
.commit-label input:focus, .commit-label textarea:focus {
  outline: none; border-color: #0366d6; box-shadow: 0 0 0 2px rgba(3,102,214,0.2);
}
.commit-actions { display: flex; gap: 0.6em; align-items: center; flex-wrap: wrap; }
.commit-actions button { padding: 0.45em 1em; border: 1px solid #d0d7de; border-radius: 4px;
                          background: #fff; color: #0366d6; cursor: pointer;
                          font: inherit; font-size: 0.88em; font-weight: 600; }
.commit-actions button:hover { background: #ddf4ff; }
.commit-actions button:disabled { color: #8c959f; cursor: not-allowed; background: #f6f8fa; }
.commit-actions #btn-commit-push { background: #2da44e; color: #fff; border-color: #2c974b; }
.commit-actions #btn-commit-push:hover { background: #2c974b; }
.copy-feedback { font-size: 0.85em; color: #1a7f37; min-height: 1.2em; }
.copy-feedback.error { color: #d73a49; }
.commit-preview { margin-top: 0.8em; font-size: 0.85em; }
.commit-preview summary { cursor: pointer; color: #0366d6; }
.commit-preview pre { background: #fff; border: 1px solid #d0d7de; border-radius: 4px;
                       padding: 0.6em 0.8em; margin-top: 0.4em; font-size: 0.85em;
                       font-family: ui-monospace, monospace; white-space: pre-wrap;
                       word-break: break-all; }
code { background: #f6f8fa; padding: 0.1em 0.4em; border-radius: 3px;
       font-family: ui-monospace, monospace; font-size: 0.9em; }
"""


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
<style>{css}</style>
</head>
<body>
<header>
<h1>{title}</h1>
<div class="meta">
分支 <strong>{branch}</strong> · HEAD <code>{head}</code> · 模式 <strong>{mode}</strong> · 生成于 {generated_at}
</div>
<div class="stats">
<span><strong>{file_count}</strong> 个文件改动</span>
<span class="add-count">+{added}</span>
<span class="del-count">−{deleted}</span>
</div>
</header>

{scope_note}

{validation_block}

{summary_block}

{commit_section}

<section>
<h2>改动 · 意义</h2>
{file_rows}
</section>

{risks_section}

<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script>
  if (window.hljs) {{
    document.querySelectorAll('code[class^="language-"]').forEach(function (el) {{
      try {{ hljs.highlightElement(el); }} catch (e) {{ /* ignore */ }}
    }});
  }}

  // Commit box: build git commit command from current subject/body, copy to clipboard.
  (function () {{
    var commitBox = document.querySelector('.commit-box');
    var subjectEl = document.getElementById('commit-subject');
    var bodyEl = document.getElementById('commit-body');
    var btnCommit = document.getElementById('btn-commit');
    var btnPush = document.getElementById('btn-commit-push');
    var feedback = document.getElementById('copy-feedback');
    var previewEl = document.getElementById('commit-preview-text');
    if (!commitBox || !subjectEl || !btnCommit || !btnPush) return;
    // Workspace mode (no staged content) needs `git add -A` first.
    var needsAdd = commitBox.dataset.staged === 'false';

    function shellEscape(s) {{
      // Wrap content in double quotes safely: escape \\ " $ `
      return s.replace(/\\\\/g, '\\\\\\\\')
              .replace(/"/g, '\\\\"')
              .replace(/\\$/g, '\\\\$')
              .replace(/`/g, '\\\\`');
    }}

    function buildCommand(withPush) {{
      var subject = (subjectEl.value || '').trim();
      var body = (bodyEl.value || '').replace(/\\r\\n/g, '\\n').replace(/\\s+$/, '');
      if (!subject) return null;
      var msg = subject;
      if (body) msg += '\\n\\n' + body;
      var cmd = '';
      if (needsAdd) cmd = 'git add -A && ';
      cmd += 'git commit -m "' + shellEscape(msg) + '"';
      if (withPush) cmd += ' && git push';
      return cmd;
    }}

    function updatePreview() {{
      var cmd = buildCommand(false);
      previewEl.textContent = cmd || '(请填写 subject)';
      var has = !!cmd;
      btnCommit.disabled = !has;
      btnPush.disabled = !has;
    }}

    function showFeedback(msg, isError) {{
      feedback.textContent = msg;
      feedback.classList.toggle('error', !!isError);
      clearTimeout(feedback._t);
      feedback._t = setTimeout(function () {{ feedback.textContent = ''; }}, 2500);
    }}

    function copy(text) {{
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        return navigator.clipboard.writeText(text);
      }}
      // Fallback for non-secure contexts (file://)
      return new Promise(function (resolve, reject) {{
        try {{
          var ta = document.createElement('textarea');
          ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
          document.body.appendChild(ta); ta.select();
          var ok = document.execCommand('copy');
          document.body.removeChild(ta);
          ok ? resolve() : reject(new Error('execCommand failed'));
        }} catch (e) {{ reject(e); }}
      }});
    }}

    function onClick(withPush) {{
      var cmd = buildCommand(withPush);
      if (!cmd) {{ showFeedback('请先填写 subject', true); return; }}
      copy(cmd).then(
        function () {{ showFeedback('✓ 已复制：' + (withPush ? 'commit & push' : 'commit') + ' 命令'); }},
        function (e) {{ showFeedback('复制失败：' + e.message, true); }}
      );
    }}

    btnCommit.addEventListener('click', function () {{ onClick(false); }});
    btnPush.addEventListener('click', function () {{ onClick(true); }});
    subjectEl.addEventListener('input', updatePreview);
    bodyEl.addEventListener('input', updatePreview);
    updatePreview();
  }})();

  // Foldable diff: cap diff-col height to analysis-col height
  document.querySelectorAll('.file-row').forEach(function (row) {{
    var analysisCol = row.querySelector('.analysis-col');
    var diffCol = row.querySelector('.diff-col');
    if (!analysisCol || !diffCol) return;

    var target = analysisCol.offsetHeight;
    if (target <= 0) return;
    diffCol.style.maxHeight = target + 'px';

    if (diffCol.scrollHeight <= target + 4) return;

    diffCol.classList.add('clipped');
    var btn = document.createElement('button');
    btn.className = 'diff-toggle';
    btn.type = 'button';
    btn.textContent = '展开代码 ▾';
    btn.setAttribute('aria-expanded', 'false');
    diffCol.appendChild(btn);

    btn.addEventListener('click', function () {{
      var expanded = diffCol.classList.toggle('expanded');
      diffCol.style.maxHeight = expanded ? 'none' : (target + 'px');
      btn.textContent = expanded ? '收起代码 ▴' : '展开代码 ▾';
      btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      if (expanded) {{
        // After expand the button stays absolute at diff-col bottom — scroll
        // it into view so the user can confirm position changed.
        btn.scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
      }}
    }});
  }});
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Visualize uncommitted git changes as HTML.")
    parser.add_argument("--analysis", help="Path to analysis JSON file (optional).")
    parser.add_argument("--output", help="Output HTML path. Default: <repo>/tmp/git-diff.html")
    args = parser.parse_args()

    staged = has_staged()
    mode = "staged (暂存区)" if staged else "working tree (工作区)"

    diff_text = get_diff(staged)
    stats = get_stats(staged)
    untracked_data = get_untracked_with_content(staged)
    excluded = get_excluded_counts(staged)
    meta = get_meta()

    diff_files = parse_diff_by_file(diff_text)
    diff_by_path = dict(diff_files)
    rows_data = [
        {
            "path": f["path"],
            "binary": f["binary"],
            "added": f["added"],
            "deleted": f["deleted"],
            "diff_html": diff_by_path.get(f["path"]),
            "untracked": False,
        }
        for f in stats["files"]
    ]
    rows_data.extend(untracked_to_row(u) for u in untracked_data)

    file_count = len(rows_data)
    total_added = sum(r["added"] for r in rows_data)
    total_deleted = sum(r["deleted"] for r in rows_data)

    print(f"[scope] mode={mode}, files={file_count}"
          f" (tracked={len(stats['files'])}, untracked={len(untracked_data)})",
          file=sys.stderr)
    if excluded:
        print(
            f"[scope] ignoring {excluded['unstaged_files']} unstaged file(s) "
            f"and {excluded['untracked_files']} untracked file(s)",
            file=sys.stderr,
        )

    analysis = None
    validation_errors = []
    if args.analysis:
        p = Path(args.analysis)
        if p.exists():
            analysis = json.loads(p.read_text(encoding="utf-8"))
            scope_paths = {r["path"] for r in rows_data}
            validation_errors = validate_analysis(analysis, scope_paths)
            if validation_errors:
                print("[analysis] validation errors:", file=sys.stderr)
                for e in validation_errors:
                    print(f"  - {e}", file=sys.stderr)
        else:
            print(f"warning: analysis file {p} not found, skipping", file=sys.stderr)

    scope_note = ""
    if excluded and (excluded["unstaged_files"] or excluded["untracked_files"]):
        scope_note = (
            f'<div class="scope-note">⚠ 作用域：仅暂存区。'
            f'已忽略 <strong>{excluded["unstaged_files"]}</strong> 个未暂存文件、'
            f'<strong>{excluded["untracked_files"]}</strong> 个未跟踪文件——'
            f'分析与左侧 diff 都不包含它们。</div>'
        )

    html_out = HTML_TEMPLATE.format(
        title=html.escape(page_title(analysis)),
        branch=html.escape(meta["branch"]),
        head=html.escape(meta["head"]),
        mode=mode,
        generated_at=meta["generated_at"],
        file_count=file_count,
        added=total_added,
        deleted=total_deleted,
        css=CSS,
        scope_note=scope_note,
        validation_block=render_validation_errors(validation_errors),
        summary_block=render_summary(analysis),
        commit_section=render_commit_box(analysis, staged),
        file_rows=render_file_rows(rows_data, analysis),
        risks_section=render_risks(analysis),
    )

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = get_repo_root() / "tmp" / "git-diff.html"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_out, encoding="utf-8")
    output_url = "file://" + quote(str(output_path.resolve()))
    print(output_url)


if __name__ == "__main__":
    main()
