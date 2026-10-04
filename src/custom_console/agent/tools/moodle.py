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
        confirm_login=lambda text: bool(ctx.gate.ask(text)),
    )
    ctx.on_close(runner.close)

    @guarded(ctx, PermissionLevel.READ)
    def moodle_list_courses(force_refresh: bool = False) -> ToolResult:
        """List every Moodle course visible to the user, with its numeric id.

        Always call this first to resolve a course id from its name before any
        tool needing a course_id: never guess or scrape ids.

        Args:
            force_refresh: bypass the 24h cache.

        Returns a list of {id, title, url}.
        """
        if not force_refresh:
            cached = ctx.cache.get(COURSES_CACHE_KEY, max_age=COURSES_CACHE_TTL)
            if cached is not None:
                return ToolResult.ok(cached)
        courses = runner.run(lambda moodle: moodle.list_courses())
        ctx.cache.set(COURSES_CACHE_KEY, courses)
        return ToolResult.ok(courses)

    @guarded(ctx, PermissionLevel.READ)
    def moodle_get_course_structure(course_id: str) -> ToolResult:
        """Extract the sections, resources and visible due dates of a course.

        Args:
            course_id: numeric Moodle course id (from `moodle_list_courses`; never guess it).

        Returns text: one "##" line per section, then one line per resource.
        """
        data = runner.run(lambda moodle: moodle.get_course_structure(course_id))
        return ToolResult.ok(compact_course_structure(data))

    @guarded(ctx, PermissionLevel.READ)
    def moodle_get_page_content(
        url: str,
        selector: Optional[str] = None,
        include_html: bool = False,
        max_chars: Optional[int] = 8000,
    ) -> ToolResult:
        """Open a Moodle URL and return the cleaned text (or HTML) of an area.

        By default the main content region is used (not the whole <body>):
        navigation, side blocks and scripts are stripped.

        Args:
            url: absolute, or relative to the Moodle base (e.g. "/my/").
            selector: CSS or XPath ("xpath=", "//", "..") of the area to extract.
            include_html: return the cleaned inner HTML instead of the text.
            max_chars: truncate the result (None to disable); prefer a narrower
                `selector` over raising it.

        If the URL is a file (e.g. a PDF) the answer says so: use
        `moodle_download_file` instead of retrying this tool.
        """
        text = runner.run(
            lambda moodle: moodle.get_page_content(
                url, selector=selector, include_html=include_html, max_chars=max_chars
            )
        )
        return ToolResult.ok(text)

    @guarded(ctx, PermissionLevel.READ)
    def moodle_get_announcements(limit: int = 20) -> ToolResult:
        """Fetch the announcements shown on the Moodle dashboard.

        Args:
            limit: maximum number of announcements.
        """
        items = runner.run(lambda moodle: moodle.get_announcements(limit=limit))
        return ToolResult.ok(compact_announcements(items))

    @guarded(ctx, PermissionLevel.READ)
    def moodle_get_grades() -> ToolResult:
        """Extract the rows of the Moodle grades overview. Returns one
        pipe-separated line per row."""
        rows = runner.run(lambda moodle: moodle.get_grades())
        return ToolResult.ok(compact_grades(rows))

    @guarded(ctx, PermissionLevel.WRITE)
    def moodle_click_element(selector: str, wait_until: str = "domcontentloaded") -> ToolResult:
        """Click an element of the current Moodle page.

        Args:
            selector: CSS or XPath ("xpath=", "//", "..").
            wait_until: Playwright wait condition ("domcontentloaded", "load", "networkidle").

        Returns {clicked, url, title}.
        """
        return ToolResult.ok(runner.run(lambda moodle: moodle.click_element(selector, wait_until=wait_until)))

    @guarded(ctx, PermissionLevel.WRITE)
    def moodle_input_text(selector: str, text: str, submit: bool = False) -> ToolResult:
        """Fill a text field of the current Moodle page.

        Args:
            selector: CSS or XPath ("xpath=", "//", "..").
            text: the text to type.
            submit: press Enter afterwards and wait for the navigation.

        Returns {filled, url}.
        """
        return ToolResult.ok(runner.run(lambda moodle: moodle.input_text(selector, text, submit=submit)))

    @guarded(ctx, PermissionLevel.WRITE)
    def moodle_download_file(file_url: str, save_path: str) -> ToolResult:
        """Download a file from Moodle with the active session.

        Args:
            file_url: URL to download, absolute or relative to the Moodle base.
            save_path: local file to create, e.g. "course/lecture1.pdf".

        Returns {downloaded, path, suggested_filename, failure}.
        """
        target = ctx.files.local_path(save_path, "moodle_download_file")
        ctx.snapshot(target)
        result = runner.run(lambda moodle: moodle.download_file(file_url, target))
        result["path"] = target
        if result.get("downloaded"):
            ctx.reads.mark(target)
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
