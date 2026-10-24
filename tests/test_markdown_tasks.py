import pytest

from tracker.markdown_tasks import match_tasks, parse_tasks, patch_tasks


def bindings(doc):
    return [
        {"item_id": f"item-{index}", "line": task.line, "title": task.title,
         "checked": task.checked}
        for index, task in enumerate(doc.tasks)
    ]


def match(before, after):
    base, current = parse_tasks(before), parse_tasks(after)
    return match_tasks(base, current, bindings(base))


def test_parse_real_task_lists_preserves_every_byte_and_heading():
    data = (
        b"# Farm\r\n\r\n## Today\r\n\r\n"
        b"* [X] parent **bold**  \r\n  ordinary notes\r\n"
        b"  + [ ] child `code`\r\n\r\n"
        b"  ```markdown\r\n  - [ ] not a task\r\n  ```\r\n\r\n"
        b"> 1. [x] quoted task\r\n\r\n"
        b"Later\r\n-----\r\n\r\n"
        b"1) [ ] ordered\r\n\r\nA code example:\r\n\r\n"
        b"    - [ ] indented code\r\n\r\n"
        b"<!--\r\n- [ ] commented out\r\n-->\r\n\r\n"
        b"~~~\r\n- [ ] fenced\r\n~~~\r\n"
        b"- [-] nonstandard\r\n- [ ] "
    )
    doc = parse_tasks(data)
    assert [t.title for t in doc.tasks] == [
        "parent **bold**", "child `code`", "quoted task", "ordered"
    ]
    assert doc.tasks[0].heading_path == ("Farm", "Today")
    assert doc.tasks[-1].heading_path == ("Farm", "Later")
    assert [t.checked for t in doc.tasks] == [True, False, True, False]
    assert patch_tasks(doc, {}) == data
    assert patch_tasks(doc, {doc.tasks[0].line: {"checked": True}}) == data


def test_patch_unicode_title_and_checkbox_only():
    data = "# Things\n\n\t* [X]\t café **now**  \n".encode()
    assert not parse_tasks(data).tasks
    data = "# Things\n\n* [X]\tcafé **now**  \n  note\n  - [ ] child".encode()
    doc = parse_tasks(data)
    result = patch_tasks(doc, {2: {"title": "naïve `new`", "checked": False}})
    assert result == "# Things\n\n* [ ]\tnaïve `new`  \n  note\n  - [ ] child".encode()


def test_remove_checkbox_preserves_notes_children_and_bullet():
    data = b"- [x] parent\n  text\n  - [ ] child\n"
    assert patch_tasks(parse_tasks(data), {0: {"remove_checkbox": True}}) == (
        b"- parent\n  text\n  - [ ] child\n"
    )


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n", b"\r"])
def test_append_keeps_original_prefix_and_line_endings(ending):
    data = b"# Tasks" + ending
    result = patch_tasks(parse_tasks(data), {}, [{"title": "New", "checked": True}])
    newline = ending or b"\n"
    assert result == b"# Tasks" + newline * 2 + b"- [x] New" + newline


def test_append_cannot_disappear_into_unclosed_fence():
    with pytest.raises(ValueError, match="block structure"):
        patch_tasks(parse_tasks(b"```\ncode\n"), {}, [{"title": "New"}])


@pytest.mark.parametrize("title", ["", "\nnew", "old\rnew", "\x00", " leading", "trailing "])
def test_title_cannot_inject_or_erase_lines(title):
    with pytest.raises(ValueError):
        patch_tasks(parse_tasks(b"- [ ] old\n"), {0: {"title": title}})


def test_matching_reorder_and_status_change_keeps_ids():
    result = match(b"- [ ] A\n- [x] B\n", b"# Now\n\n- [ ] B\n- [x] A\n")
    assert {key: (t.line, t.checked) for key, t in result.matches.items()} == {
        "item-0": (3, True), "item-1": (2, False)
    }
    assert not result.additions and not result.removals and not result.ambiguities


def test_matching_single_row_rename_uses_context():
    result = match(
        b"# Tasks\n\n- [ ] A\n  notes\n- [ ] B\n",
        b"# Tasks\n\n- [x] Renamed\n  notes\n- [ ] B\n",
    )
    assert result.matches["item-0"].title == "Renamed"
    assert result.matches["item-0"].checked
    assert not result.ambiguities


def test_separate_addition_or_removal_is_unambiguous():
    added = match(b"- [ ] A\n", b"- [ ] New\n- [ ] A\n")
    assert [t.title for t in added.additions] == ["New"]
    removed = match(b"- [ ] A\n- [ ] B\n", b"- [ ] B\n")
    assert removed.removals == ("item-0",)
    assert not removed.additions and not removed.ambiguities


def test_duplicate_change_cannot_guess_which_item_was_edited():
    result = match(b"- [ ] Same\n- [ ] Same\n", b"- [x] Same\n- [ ] Same\n")
    assert result.ambiguities == ("item-0", "item-1")
    assert not result.matches and not result.additions and not result.removals


def test_duplicate_count_change_cannot_create_or_cancel_wrong_task():
    for before, after in (
        (b"- [ ] Same\n", b"- [ ] Same\n- [ ] Same\n"),
        (b"- [ ] Same\n- [ ] Same\n", b"- [ ] Same\n"),
        (b"- [ ] Same\n- [ ] Same\n", b"- [ ] Same\n- [ ] Renamed\n"),
    ):
        result = match(before, after)
        assert result.ambiguities
        assert not result.matches and not result.additions and not result.removals


def test_unchanged_duplicates_survive_note_edits_without_binding_order_dependence():
    base = parse_tasks(b"- [ ] Same\n- [ ] Same\n\nnotes\n")
    current = parse_tasks(b"# Heading\n\n- [ ] Same\n- [ ] Same\n\nnotes\n")
    result = match_tasks(base, current, list(reversed(bindings(base))))
    assert result.ambiguities
    current = parse_tasks(b"new note\n\n- [ ] Same\n- [ ] Same\n\nnotes\n")
    result = match_tasks(base, current, list(reversed(bindings(base))))
    assert result.matches["item-0"].line == 2
    assert result.matches["item-1"].line == 3


def test_reordering_distinct_notes_on_duplicate_titles_cannot_swap_tracker_ids():
    result = match(
        b"- [ ] Same\n  Note A\n\n- [ ] Same\n  Note B\n",
        b"- [ ] Same\n  Note B\n\n- [ ] Same\n  Note A\n",
    )
    assert result.ambiguities == ("item-0", "item-1")
    assert not result.matches and not result.additions and not result.removals


def test_duplicate_tasks_moving_across_unique_anchors_are_ambiguous():
    result = match(
        b"- [ ] Same\n- [ ] Anchor\n- [ ] Same\n",
        b"- [ ] Anchor\n- [ ] Same\n- [ ] Same\n",
    )
    assert result.ambiguities == ("item-0", "item-2")
    assert result.matches["item-1"].title == "Anchor"
    assert not result.additions and not result.removals


def test_multiple_rewrites_or_rename_and_move_pause_affected_tasks():
    for before, after in (
        (b"- [ ] A\n- [ ] B\n", b"- [ ] C\n- [ ] D\n"),
        (b"## First\n- [ ] A\n\n## Second\n", b"## First\n\n## Second\n- [ ] Renamed\n"),
    ):
        result = match(before, after)
        assert result.ambiguities
        assert not result.additions and not result.removals


def test_bad_bindings_fail_closed():
    doc = parse_tasks(b"- [ ] Task\n")
    for value in (
        [{"item_id": "a", "line": 8}],
        [{"item_id": "a", "line": 0, "title": "wrong"}],
        [{"item_id": "a", "line": 0}, {"item_id": "b", "line": 0}],
    ):
        with pytest.raises(ValueError, match="binding"):
            match_tasks(doc, doc, value)


def test_long_titles_are_not_truncated_and_invalid_document_rejected():
    title = "long " * 99 + "title"
    assert parse_tasks(("- [ ] " + title + "\n").encode()).tasks[0].title == title
    for data in (b"\xff", b"\x00", b"a" * (1024 * 1024 + 1)):
        with pytest.raises((ValueError, UnicodeError)):
            parse_tasks(data)
