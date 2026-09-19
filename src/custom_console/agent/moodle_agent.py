from __future__ import annotations

import json
import re

from typing import Any, Optional

from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


@dataclass
class Resource:
    title: str
    url: str | None = None
    due_date: str | None = None
    kind: str | None = None


@dataclass
class Course:
    id: str
    title: str
    url: str


@dataclass
class CourseSection:
    title: str
    resources: list[Resource]


class MoodleAgent:
    """Agent Moodle basé sur Playwright.

    La première exécution ouvre un navigateur pour permettre la connexion SSO
    manuelle. L'état de session est ensuite enregistré dans state_path.
    """

    def __init__(
        self,
        base_url: str = "https://moodle.epita.fr",
        state_path: Optional[str] = None,
        headless: bool = False,
        timeout_ms: int = 20_000,
        sso_button_selector: str = (
            "button:has-text('Connexion'), button:has-text('Se connecter'), "
            "a:has-text('Connexion'), a:has-text('Se connecter'), a:has-text('Forge ID'), "
            "input[type=submit]"
        ),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not state_path:
            self.state_path = None
        else:
            self.state_path = Path(state_path)
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.sso_button_selector = sso_button_selector
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._user_id: str | None = None

    def start(self, interactive_login: bool = True) -> None:
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless)

        context_options: dict[str, Any] = {
            "accept_downloads": True,
            "viewport": {"width": 1440, "height": 1000},
        }
        if self.state_path.exists():
            context_options["storage_state"] = str(self.state_path)

        self.context = self.browser.new_context(**context_options)
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        # "networkidle" est nécessaire ici (et pas seulement "domcontentloaded") :
        # avec une session valide, Moodle peut passer par une redirection SSO
        # intermédiaire avant de revenir sur "/my/". Vérifier l'URL trop tôt
        # ferait croire à tort que la session a expiré.
        self.page.goto(f"{self.base_url}/my/", wait_until="networkidle")

        if self._is_login_page():
            self._try_auto_sso_click()

        if self._is_login_page():
            if not interactive_login:
                raise RuntimeError("La session Moodle est absente ou expirée.")
            if self.headless:
                raise RuntimeError(
                    "Une connexion SSO initiale nécessite headless=False."
                )
            input("Connecte-toi dans le navigateur, puis appuie sur Entrée ici... ")
            self._ensure_authenticated()
            self.context.storage_state(path=str(self.state_path))
        else:
            self._ensure_authenticated()
            # La session a peut-être été rafraîchie via le SSO automatique
            # (ou était encore valide) : on sauvegarde l'état à jour.
            self.context.storage_state(path=str(self.state_path))

    def _try_auto_sso_click(self) -> None:
        """Best-effort: auto-clicks the school's one-click SSO login button.

        When the Moodle session expires, the school's SSO (cri.epita.fr)
        sometimes just needs a single button click to re-authenticate
        (e.g. because the underlying institutional session is still valid),
        with no credentials to type. This tries to find and click that
        button automatically so `start()` doesn't stop and wait on `input()`
        for something the user doesn't actually need to do by hand.

        If no matching button is found, or the click doesn't lead off the
        login page, this silently does nothing and `start()` falls back to
        the interactive flow.
        """
        page = self._require_page()
        try:
            button = page.locator(self.sso_button_selector).first
            if button.count() == 0:
                return
            with page.expect_navigation(
                wait_until="networkidle", timeout=self.timeout_ms
            ):
                button.click(timeout=self.timeout_ms)
        except PlaywrightTimeoutError:
            pass

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
        if self.browser is not None:
            self.browser.close()
        if self._pw is not None:
            self._pw.stop()
        self.page = None
        self.context = None
        self.browser = None
        self._pw = None

    def __enter__(self) -> "MoodleAgent":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _require_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("Appelle start() avant d'utiliser l'agent.")
        return self.page

    def _require_context(self) -> BrowserContext:
        if self.context is None:
            raise RuntimeError("Appelle start() avant d'utiliser l'agent.")
        return self.context

    def _is_login_page(self) -> bool:
        page = self._require_page()
        return "cri.epita.fr" in page.url or "/login/" in page.url

    def _ensure_authenticated(self) -> None:
        if self._is_login_page():
            raise RuntimeError(
                f"Authentification non terminée : {self._require_page().url}"
            )

    def _locator(self, selector: str):
        page = self._require_page()
        if (
            selector.startswith("xpath=")
            or selector.startswith("//")
            or selector.startswith("..")
        ):
            return page.locator(
                selector if selector.startswith("xpath=") else f"xpath={selector}"
            )
        return page.locator(selector)

    # Sélecteurs Moodle usuels contenant le contenu utile d'une page (dans
    # l'ordre de préférence), utilisés quand l'appelant ne précise pas de
    # `selector`. On évite ainsi de renvoyer tout `<body>` (nav, blocs
    # latéraux, pied de page...).
    _MAIN_CONTENT_SELECTORS = (
        "#region-main",
        "[role='main']",
        "#page-content",
        ".content-main",
        "main",
    )

    # Éléments purement décoratifs / de navigation à retirer avant extraction,
    # même quand on cible déjà une zone précise (ils peuvent être imbriqués
    # dans le contenu principal : blocs latéraux Moodle, etc.).
    _NOISE_SELECTORS = (
        "script",
        "style",
        "noscript",
        "svg",
        "nav",
        "header",
        "footer",
        ".navbar",
        "#nav-drawer",
        ".drawer",
        ".secondary-navigation",
        ".breadcrumb",
        ".block",
        ".block_list",
        ".sidebar",
        "[data-region='drawer']",
        ".skip-block",
        ".skiplinks",
    )

    def get_page_content(
        self,
        url: str,
        selector: str | None = None,
        include_html: bool = False,
        max_chars: int | None = 8000,
    ) -> str:
        """Navigue vers une URL et renvoie le texte (ou HTML) utile d'une zone.

        Si `selector` n'est pas fourni, on cherche automatiquement la zone de
        contenu principal de Moodle (`#region-main`, `[role='main']`, etc.)
        plutôt que de prendre tout `<body>`. Dans tous les cas, les éléments
        de pure navigation/décoration (menus, blocs latéraux, scripts,
        styles, pied de page...) sont retirés avant extraction, pour ne
        renvoyer que ce qui est effectivement utile.

        `max_chars` tronque le résultat (texte ou HTML) pour éviter de noyer
        l'appelant avec une page inhabituellement longue ; mets `None` pour
        désactiver la troncature.

        Si l'URL pointe directement sur un fichier non-HTML (typiquement un
        PDF, ouvert par la visionneuse intégrée de Chromium plutôt que rendu
        comme une vraie page), il n'y a pas de vrai `<body>` exploitable :
        plutôt que de planter en attendant un élément qui n'apparaîtra
        jamais, on détecte ce cas via l'en-tête `content-type` de la réponse
        et on renvoie un message explicite invitant à utiliser
        `download_file` à la place.
        """
        page = self._require_page()
        full_url = urljoin(self.base_url + "/", url)
        response = page.goto(full_url, wait_until="domcontentloaded")
        self._ensure_authenticated()

        content_type = (response.headers.get("content-type") if response else "") or ""
        if "html" not in content_type.lower():
            return (
                f"[Contenu non affichable : cette URL renvoie directement un "
                f"fichier de type '{content_type or 'inconnu'}' plutôt qu'une "
                f"page HTML.] Utilise l'outil de téléchargement (download_file) "
                f"pour récupérer ce fichier : {full_url}"
            )

        page.wait_for_load_state("networkidle")

        target = self._locator(selector) if selector else self._find_main_content()
        target.wait_for(state="visible")
        raw_html = target.evaluate("element => element.outerHTML")
        cleaned_html = self._strip_noise(raw_html)

        if include_html:
            result = cleaned_html
        else:
            soup = BeautifulSoup(cleaned_html, "html.parser")
            result = self._clean_text(soup.get_text("\n"))

        if max_chars is not None and len(result) > max_chars:
            result = (
                result[:max_chars]
                + f"\n[... contenu tronqué à {max_chars} caractères ; utilise "
                "`selector` pour cibler une sous-partie plus précise si "
                "besoin.]"
            )
        return result

    def _find_main_content(self):
        """Renvoie le premier des `_MAIN_CONTENT_SELECTORS` présent sur la
        page, ou `body` si aucun ne matche."""
        page = self._require_page()
        for candidate in self._MAIN_CONTENT_SELECTORS:
            locator = page.locator(candidate)
            if locator.count() > 0:
                return locator.first
        return page.locator("body")

    @classmethod
    def _strip_noise(cls, html: str) -> str:
        """Retire du HTML les éléments de navigation/décoration inutiles."""
        soup = BeautifulSoup(html, "html.parser")
        for selector in cls._NOISE_SELECTORS:
            for node in soup.select(selector):
                node.decompose()
        return str(soup)

    def click_element(
        self, selector: str, wait_until: str = "domcontentloaded"
    ) -> dict[str, Any]:
        """Clique sur un élément et renvoie l'URL finale."""
        page = self._require_page()
        locator = self._locator(selector)
        locator.wait_for(state="visible")
        try:
            with page.expect_navigation(wait_until=wait_until, timeout=self.timeout_ms):
                locator.click()
        except PlaywrightTimeoutError:
            locator.click()
        return {"clicked": True, "url": page.url, "title": page.title()}

    def input_text(
        self, selector: str, text: str, submit: bool = False
    ) -> dict[str, Any]:
        """Remplit un champ. submit=True appuie ensuite sur Entrée."""
        page = self._require_page()
        locator = self._locator(selector)
        locator.wait_for(state="visible")
        locator.fill(text)
        if submit:
            locator.press("Enter")
            page.wait_for_load_state("domcontentloaded")
        return {"filled": True, "url": page.url}

    def list_courses(self) -> list[dict[str, Any]]:
        """List every course visible to the user, with its Moodle id.

        This is the way to resolve a course's numeric id from its name.
        It reads the "Course profiles" table shown on the user's own
        profile page with "showallcourses=1" (which, unlike the dashboard,
        is not affected by the "En cours" / "Retirés de l'affichage" filter
        and always lists every course, including hidden ones), and pairs
        each course title with its id, so that `get_course_structure` and
        course URLs can be built without guessing an id from an arbitrary
        page.
        """
        user_id = self._get_own_user_id()
        return self._scan_course_links(
            f"/user/profile.php?id={user_id}&showallcourses=1"
        )

    def _get_own_user_id(self) -> str:
        """Resolves and caches the logged-in user's numeric Moodle id.

        It is read from the profile link in the dashboard's user menu
        (present in every standard Moodle theme once authenticated).
        """
        if self._user_id is not None:
            return self._user_id

        page = self._require_page()
        page.goto(f"{self.base_url}/my/", wait_until="networkidle")
        self._ensure_authenticated()

        link = page.locator("a[href*='/user/profile.php?id=']").first
        if link.count() == 0:
            raise RuntimeError(
                "Impossible de déterminer l'id utilisateur Moodle : aucun "
                "lien vers /user/profile.php trouvé sur /my/."
            )

        href = link.get_attribute("href") or ""
        user_id = parse_qs(urlparse(href).query).get("id", [None])[0]
        if not user_id:
            raise RuntimeError(f"URL de profil inattendue : {href!r}.")

        self._user_id = user_id
        return user_id

    def _scan_course_links(self, path: str) -> list[dict[str, Any]]:
        page = self._require_page()
        page.goto(urljoin(self.base_url + "/", path), wait_until="networkidle")
        self._ensure_authenticated()

        soup = BeautifulSoup(page.content(), "html.parser")

        courses: list[Course] = []
        seen: set[str] = set()
        for link in soup.select("a[href*='/user/view.php?id=']"):
            href = link.get("href")
            title = self._clean_text(link.get_text(" ", strip=True))
            if not href or not title:
                continue
            _t = urljoin(self.base_url + "/", href)
            course_id = parse_qs(urlparse(_t).query).get("course", [None])[0]
            full_url = urljoin(self.base_url, "/course/view.php?id=" + course_id)
            if not course_id or course_id in seen:
                continue
            seen.add(course_id)
            courses.append(Course(course_id, title, full_url))

        return [asdict(c) for c in courses]

    def get_course_structure(self, course_id: str) -> dict[str, Any]:
        """Extrait les sections, ressources et dates visibles d'un cours."""
        page = self._require_page()
        url = f"{self.base_url}/course/view.php?id={course_id}"
        page.goto(url, wait_until="domcontentloaded")
        self._ensure_authenticated()
        page.wait_for_load_state("networkidle")

        sections = []
        section_nodes = page.locator(
            "li.section, .course-section, [data-for='section']"
        )
        for i in range(section_nodes.count()):
            node = section_nodes.nth(i)
            title_locator = node.locator(
                ".sectionname, .sectionname a, .course-section-header, h3, h4"
            ).first
            title = self._safe_inner_text(title_locator) or f"Section {i + 1}"
            resources: list[Resource] = []

            for j in range(node.locator("a[href]").count()):
                link = node.locator("a[href]").nth(j)
                text = self._clean_text(self._safe_inner_text(link))
                href = link.get_attribute("href")
                if (
                    not text
                    or not href
                    or "sectionname" in (link.get_attribute("class") or "")
                ):
                    continue
                if href.startswith("#"):
                    continue
                resource_kind = self._resource_kind(
                    href, link.get_attribute("class") or ""
                )
                due_date = self._extract_due_date(node)
                resources.append(
                    Resource(
                        text,
                        urljoin(self.base_url + "/", href),
                        due_date,
                        resource_kind,
                    )
                )

            sections.append(CourseSection(title, self._unique_resources(resources)))

        return {
            "course_id": str(course_id),
            "url": page.url,
            "sections": [
                {"title": s.title, "resources": [asdict(r) for r in s.resources]}
                for s in sections
            ],
        }

    def get_announcements(self, limit: int = 20) -> list[dict[str, Any]]:
        """Récupère les annonces visibles sur le tableau de bord."""
        page = self._require_page()
        page.goto(f"{self.base_url}/my/", wait_until="domcontentloaded")
        self._ensure_authenticated()
        page.wait_for_load_state("networkidle")

        candidates = page.locator(
            "article, .forumpost, .block_news_items .content li, "
            "[data-region='event-item'], .activity-item"
        )
        results = []
        seen = set()
        for i in range(min(candidates.count(), limit * 3)):
            node = candidates.nth(i)
            text = self._clean_text(self._safe_inner_text(node))
            if not text or text in seen:
                continue
            seen.add(text)
            link = node.locator("a[href]").first
            results.append(
                {
                    "title": self._safe_inner_text(link) or text.split("\n", 1)[0],
                    "text": text,
                    "url": link.get_attribute("href") if link.count() else None,
                }
            )
            if len(results) >= limit:
                break
        return results

    def get_grades(self) -> list[dict[str, Any]]:
        """Extrait les lignes du carnet de notes accessibles à l'utilisateur."""
        page = self._require_page()
        page.goto(
            f"{self.base_url}/grade/report/overview/index.php",
            wait_until="domcontentloaded",
        )
        self._ensure_authenticated()
        page.wait_for_load_state("networkidle")

        rows = page.locator("table tbody tr")
        results = []
        for i in range(rows.count()):
            cells = rows.nth(i).locator("th, td")
            values = [
                self._clean_text(cells.nth(j).inner_text())
                for j in range(cells.count())
            ]
            if values:
                results.append({"columns": values})
        return results

    def download_file(self, file_url: str, save_path: str) -> dict[str, Any]:
        """Télécharge un fichier avec la session Moodle active.

        Utilise `context.request` (une requête HTTP brute, mais authentifiée
        avec les cookies de session du contexte Playwright) plutôt qu'une
        navigation de page : pour les PDF, Chromium embarque une visionneuse
        qui intercepte la navigation et affiche le document au lieu de
        déclencher un événement de téléchargement, ce qui bloquait
        indéfiniment `page.expect_download()`. Cette approche fonctionne de
        la même façon pour tous les types de fichiers (PDF, images, archives,
        documents Office, ...).
        """
        context = self._require_context()
        destination = Path(save_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = urljoin(self.base_url + "/", file_url)

        response = context.request.get(url, timeout=self.timeout_ms)
        if not response.ok:
            return {
                "downloaded": False,
                "path": str(destination),
                "suggested_filename": None,
                "failure": f"HTTP {response.status} {response.status_text}",
            }

        destination.write_bytes(response.body())

        content_disposition = response.headers.get("content-disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", content_disposition)
        suggested_filename = match.group(1).strip() if match else destination.name

        return {
            "downloaded": True,
            "path": str(destination),
            "suggested_filename": suggested_filename,
            "failure": None,
        }

    @staticmethod
    def _clean_text(text: str) -> str:
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()

    @staticmethod
    def _safe_inner_text(locator) -> str:
        try:
            return locator.inner_text(timeout=2_000) if locator.count() else ""
        except PlaywrightTimeoutError:
            return ""

    @staticmethod
    def _extract_due_date(node) -> str | None:
        text = MoodleAgent._clean_text(MoodleAgent._safe_inner_text(node))
        match = re.search(
            r"(?:Due|À rendre|Échéance|Date limite)\s*:?\s*(.+)", text, re.I
        )
        return match.group(1).split("\n", 1)[0].strip() if match else None

    @staticmethod
    def _resource_kind(href: str, classes: str) -> str:
        value = f"{href} {classes}".lower()
        for name in (
            "assign",
            "forum",
            "resource",
            "url",
            "quiz",
            "lesson",
            "page",
            "folder",
        ):
            if name in value:
                return name
        return "activity"

    @staticmethod
    def _unique_resources(resources: list[Resource]) -> list[Resource]:
        seen = set()
        result = []
        for resource in resources:
            key = (resource.title, resource.url)
            if key not in seen:
                seen.add(key)
                result.append(resource)
        return result


if __name__ == "__main__":
    with MoodleAgent(headless=False, state_path="moodle_state.json") as agent:
        print(
            agent.get_page_content(
                "https://moodle.epita.fr/mod/resource/view.php?id=106861"
            )
        )
        """agent.download_file(
            "https://moodle.epita.fr/mod/resource/view.php?id=106861", "td.pdf"
        )
        print(agent.list_courses())
        print(agent.get_page_content("/my/", selector="main"))
        print(json.dumps(agent.get_announcements(), ensure_ascii=False, indent=2))
        print(json.dumps(agent.get_grades(), ensure_ascii=False, indent=2)"""
        # print(json.dumps(agent.get_course_structure("123"), ensure_ascii=False, indent=2))
        # agent.download_file("/pluginfile.php/.../document.pdf", "downloads/document.pdf")
