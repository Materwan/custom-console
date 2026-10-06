"""`moodle_*` tools (Playwright-driven, run on a dedicated thread)."""

from typing import Any, Callable, Dict, List, Optional

from ..moodle import MoodleRunner
from ..permissions import PermissionLevel
from ..results import ToolResult
from .base import ToolContext, guarded

COURSES_CACHE_KEY = "moodle_courses"
COURSES_CACHE_TTL = 24 * 3600  # seconds


def compact_course_structure(data: Dict[str, Any]) -> str:
    """Condense a course structure into text instead of nested JSON.

    One line per resource carries the same information as the repeated
    'title'/'url'/'due_date'/'kind' keys, with far fewer tokens.
    """
    lines = [f"Course {data.get('course_id')} - {data.get('url')}"]
    for section in data.get("sections", []):
        lines.append(f"## {section.get('title')}")
        for resource in section.get("resources", []):
            due = f" (due: {resource['due_date']})" if resource.get("due_date") else ""
            lines.append(
                f"- [{resource.get('kind')}] {resource.get('title')}{due} - {resource.get('url')}"
            )
    return "\n".join(lines)


def compact_announcements(items: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for item in items:
        lines.append(f"### {item.get('title')} - {item.get('url')}")
        lines.append(item.get("text", ""))
    return "\n".join(lines)


def compact_grades(rows: List[Dict[str, Any]]) -> str:
    return "\n".join(" | ".join(row.get("columns", [])) for row in rows)


def moodle_tools(ctx: ToolContext) -> List[Callable[..., ToolResult]]:
    settings = ctx.settings
    if not settings.moodle_enabled:
        return []

    runner = MoodleRunner(
        settings.moodle_base_url,
        settings.moodle_state_path,
        confirm_login=ctx.gate.ask,
    )
    ctx.on_close(runner.close)

    @guarded(ctx, PermissionLevel.READ)
    def moodle_list_courses(force_refresh: bool = False) -> ToolResult:
        """Moodle courses {id, title, url}. Call first to get a course_id: never guess one.
        Cached 24h; force_refresh bypasses it."""
        if not force_refresh:
            cached = ctx.cache.get(COURSES_CACHE_KEY, max_age=COURSES_CACHE_TTL)
            if cached is not None:
                return ToolResult.ok(cached)
        courses = runner.run(lambda moodle: moodle.list_courses())
        ctx.cache.set(COURSES_CACHE_KEY, courses)
        return ToolResult.ok(courses)

    @guarded(ctx, PermissionLevel.READ)
    def moodle_get_course_structure(course_id: str) -> ToolResult:
        """Sections, resources and due dates of a course (id from `moodle_list_courses`)."""
        data = runner.run(lambda moodle: moodle.get_course_structure(course_id))
        return ToolResult.ok(compact_course_structure(data))

    @guarded(ctx, PermissionLevel.READ)
    def moodle_get_page_content(
        url: str,
        selector: Optional[str] = None,
        include_html: bool = False,
        max_chars: Optional[int] = 6000,
    ) -> ToolResult:
        """Cleaned text (or HTML) of a Moodle page's main area; url may be relative ("/my/").
        selector: CSS or XPath to narrow it (better than raising max_chars). A file URL
        (PDF...) is reported: use `moodle_download_file`."""
        text = runner.run(
            lambda moodle: moodle.get_page_content(
                url, selector=selector, include_html=include_html, max_chars=max_chars
            )
        )
        return ToolResult.ok(text)

    @guarded(ctx, PermissionLevel.READ)
    def moodle_get_announcements(limit: int = 10) -> ToolResult:
        """Announcements of the Moodle dashboard."""
        items = runner.run(lambda moodle: moodle.get_announcements(limit=limit))
        return ToolResult.ok(compact_announcements(items))

    @guarded(ctx, PermissionLevel.READ)
    def moodle_get_grades() -> ToolResult:
        """Grades overview, one pipe-separated line per row."""
        rows = runner.run(lambda moodle: moodle.get_grades())
        return ToolResult.ok(compact_grades(rows))

    @guarded(ctx, PermissionLevel.WRITE)
    def moodle_click_element(selector: str, wait_until: str = "domcontentloaded") -> ToolResult:
        """Click an element (CSS or XPath selector) of the current Moodle page.
        wait_until: domcontentloaded | load | networkidle."""
        return ToolResult.ok(runner.run(lambda moodle: moodle.click_element(selector, wait_until=wait_until)))

    @guarded(ctx, PermissionLevel.WRITE)
    def moodle_input_text(selector: str, text: str, submit: bool = False) -> ToolResult:
        """Type into a field (CSS or XPath selector) of the current Moodle page; submit = press Enter."""
        return ToolResult.ok(runner.run(lambda moodle: moodle.input_text(selector, text, submit=submit)))

    @guarded(ctx, PermissionLevel.WRITE)
    def moodle_download_file(file_url: str, save_directory: str, save_relative_path: str) -> ToolResult:
        """Download a Moodle file into a workspace ("tmp", or "result" for a deliverable)."""
        target = ctx.workspace.resolve(save_directory, save_relative_path)
        result = runner.run(lambda moodle: moodle.download_file(file_url, str(target)))
        result["path"] = ctx.workspace.describe(target)
        return ToolResult.ok(result)

    return [
        moodle_list_courses,
        moodle_get_course_structure,
        moodle_get_page_content,
        moodle_get_announcements,
        moodle_get_grades,
        moodle_click_element,
        moodle_input_text,
        moodle_download_file,
    ]
