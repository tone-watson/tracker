"""Read Markdown task source spans and patch them without re-rendering the document."""

import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from markdown_it import MarkdownIt


MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_TASKS = 10000
_TASK = re.compile(
    rb"^(?P<prefix>[ \t>]*(?:[-+*]|[0-9]{1,9}[.)])[ \t]+)"
    rb"\[(?P<checked>[ xX])\](?P<spacing>[ \t]+)(?P<title>.*?)(?P<trailing>[ \t]*)$"
)


@dataclass(frozen=True)
class Task:
    """A task's zero-based source line and editable byte ranges."""

    line: int
    title: str
    checked: bool
    heading_path: tuple[str, ...]
    indent: bytes
    checkbox_start: int
    title_start: int
    title_end: int
    end_line: int


@dataclass(frozen=True)
class Document:
    data: bytes
    tasks: tuple[Task, ...]
    lines: tuple[bytes, ...]


@dataclass(frozen=True)
class MatchResult:
    matches: dict[str, Task]
    additions: tuple[Task, ...]
    removals: tuple[str, ...]
    ambiguities: tuple[str, ...]


def parse_tasks(data):
    """Parse real list-item checkboxes, excluding fenced, indented and HTML code."""
    if not isinstance(data, bytes) or len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("TODO.md must be bytes of at most 1 MiB")
    source = data.decode("utf-8")
    if "\x00" in source:
        raise ValueError("TODO.md must not contain NUL bytes")
    lines = tuple(data.splitlines(keepends=True))
    tokens = MarkdownIt("commonmark").parse(source)
    headings = []
    tasks = []
    seen = set()
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            level = int(token.tag[1:])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, tokens[index + 1].content))
        if token.type != "list_item_open" or not token.map:
            continue
        line = token.map[0]
        if line in seen or line >= len(lines):
            continue
        seen.add(line)
        match = _TASK.fullmatch(lines[line].rstrip(b"\r\n"))
        if match is None or not match["title"]:
            continue
        tasks.append(
            Task(
                line=line,
                title=match["title"].decode("utf-8"),
                checked=match["checked"].lower() == b"x",
                heading_path=tuple(value for _, value in headings),
                indent=match["prefix"],
                checkbox_start=match.start("checked") - 1,
                title_start=match.start("title"),
                title_end=match.end("title"),
                end_line=token.map[1],
            )
        )
        if len(tasks) > MAX_TASKS:
            raise ValueError("TODO.md exceeds the 10000-task limit")
    return Document(data, tuple(tasks), lines)


def _binding_tasks(base, bindings):
    by_line = {task.line: task for task in base.tasks}
    result = {}
    used_lines = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ValueError("Invalid task binding")
        item_id, line = binding.get("item_id"), binding.get("line")
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id in result
            or type(line) is not int
            or line in used_lines
            or line not in by_line
        ):
            raise ValueError("Task bindings do not match the baseline snapshot")
        task = by_line[line]
        if binding.get("title", task.title) != task.title or binding.get(
            "checked", task.checked
        ) != task.checked:
            raise ValueError("Task binding content differs from its baseline snapshot")
        result[item_id] = task
        used_lines.add(line)
    return result


def match_tasks(base_doc, current_doc, bindings):
    """Match stable content and single-row edits; ambiguous identities never become creates."""
    bound = _binding_tasks(base_doc, bindings)
    if base_doc.data == current_doc.data:
        current = {task.line: task for task in current_doc.tasks}
        used = {task.line for task in bound.values()}
        return MatchResult(
            {item_id: current[task.line] for item_id, task in bound.items()},
            tuple(task for task in current_doc.tasks if task.line not in used),
            (),
            (),
        )
    old_titles, new_titles = defaultdict(list), defaultdict(list)
    for item_id, task in bound.items():
        old_titles[task.title].append((item_id, task))
    for task in current_doc.tasks:
        new_titles[task.title].append(task)
    opcodes = SequenceMatcher(
        None, base_doc.lines, current_doc.lines, autojunk=True
    ).get_opcodes()
    equal_blocks = [span for span in opcodes if span[0] == "equal"]
    anchors = {title for title, old in old_titles.items()
               if len(old) == len(new_titles[title]) == 1}

    def contexts(tasks):
        before, after, previous = {}, {}, None
        for task in tasks:
            before[task.line] = previous
            if task.title in anchors:
                previous = task.title
        following = None
        for task in reversed(tasks):
            after[task.line] = following
            if task.title in anchors:
                following = task.title
        return {task.line: (before[task.line], after[task.line]) for task in tasks}

    old_context, new_context = contexts(base_doc.tasks), contexts(current_doc.tasks)

    def unchanged_block(previous, task):
        return (
            old_context[previous.line] == new_context[task.line]
            and previous.end_line - previous.line == task.end_line - task.line
            and any(
                old_start <= previous.line and previous.end_line <= old_end
                and task.line == new_start + previous.line - old_start
                for _, old_start, old_end, new_start, _ in equal_blocks
            )
        )

    matches, ambiguous, blocked_lines = {}, set(), set()
    for title, old in old_titles.items():
        old.sort(key=lambda value: value[1].line)
        new = new_titles[title]
        if len(old) == len(new) == 1:
            matches[old[0][0]] = new[0]
        elif new:
            old_shape = [(task.checked, task.heading_path, task.indent) for _, task in old]
            new_shape = [(task.checked, task.heading_path, task.indent) for task in new]
            if old_shape == new_shape and all(
                unchanged_block(previous, task) for (_, previous), task in zip(old, new)
            ):
                for (item_id, _), task in zip(old, new):
                    matches[item_id] = task
            else:
                ambiguous.update(item_id for item_id, _ in old)
                blocked_lines.update(task.line for task in new)
    used = {task.line for task in matches.values()}
    old_left = {item_id: task for item_id, task in bound.items() if item_id not in matches}
    new_left = {task.line: task for task in current_doc.tasks if task.line not in used}
    for tag, old_start, old_end, new_start, new_end in opcodes:
        if tag != "replace":
            continue
        old = [(item_id, task) for item_id, task in old_left.items()
               if old_start <= task.line < old_end]
        new = [task for task in new_left.values() if new_start <= task.line < new_end]
        if len(old) == len(new) == 1:
            item_id, previous = old[0]
            task = new[0]
            if (
                item_id not in ambiguous
                and task.line not in blocked_lines
                and previous.heading_path == task.heading_path
                and previous.indent == task.indent
            ):
                matches[item_id] = task
                del old_left[item_id]
                del new_left[task.line]
    if old_left and new_left:
        ambiguous.update(old_left)
        blocked_lines.update(new_left)
    return MatchResult(
        matches,
        tuple(task for line, task in new_left.items() if line not in blocked_lines),
        tuple(item_id for item_id in old_left if item_id not in ambiguous),
        tuple(item_id for item_id in bound if item_id in ambiguous),
    )


def _title(value):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\x00"):
        raise ValueError("Task titles must be nonempty single-line text")
    if value != value.strip(" \t"):
        raise ValueError("Task titles must not begin or end with horizontal whitespace")
    return value.encode("utf-8")


def patch_tasks(doc, updates, appended=()):
    """Change only requested task spans; new rows are appended after existing Markdown."""
    tasks = {task.line: task for task in doc.tasks}
    lines = list(doc.lines)
    for line, update in updates.items():
        if type(line) is not int or line not in tasks or set(update) - {
            "title", "checked", "remove_checkbox"
        }:
            raise ValueError("Invalid Markdown task patch")
        task = tasks[line]
        if "checked" in update and type(update["checked"]) is not bool:
            raise ValueError("Task checked state must be boolean")
        if "remove_checkbox" in update and type(update["remove_checkbox"]) is not bool:
            raise ValueError("Task checkbox removal must be boolean")
        title = _title(update.get("title", task.title))
        original = lines[line]
        if update.get("remove_checkbox"):
            lines[line] = original[:task.checkbox_start] + title + original[task.title_end:]
        else:
            checked = update.get("checked", task.checked)
            marker = original[task.checkbox_start + 1:task.checkbox_start + 2]
            if checked != task.checked:
                marker = b"x" if checked else b" "
            lines[line] = (
                original[:task.checkbox_start + 1]
                + marker
                + original[task.checkbox_start + 2:task.title_start]
                + title
                + original[task.title_end:]
            )
    result = b"".join(lines)
    if appended:
        newline = next(
            (b"\r\n" if line.endswith(b"\r\n") else b"\r" if line.endswith(b"\r") else b"\n"
             for line in doc.lines if line.endswith((b"\r", b"\n"))),
            b"\n",
        )
        if result and not result.endswith((b"\r", b"\n")):
            result += newline
        if result and not result.endswith(newline * 2):
            result += newline
        for task in appended:
            if set(task) - {"title", "checked"} or type(task.get("checked", False)) is not bool:
                raise ValueError("Invalid appended Markdown task")
            result += b"- [" + (b"x" if task.get("checked") else b" ") + b"] "
            result += _title(task.get("title")) + newline
    parsed = parse_tasks(result)
    expected_count = len(doc.tasks) + len(appended) - sum(
        bool(update.get("remove_checkbox")) for update in updates.values()
    )
    if len(parsed.tasks) != expected_count:
        raise ValueError("Task patch would change Markdown block structure")
    return result
