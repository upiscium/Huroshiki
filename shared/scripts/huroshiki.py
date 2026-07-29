#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
from typing import Callable, Iterable

from huroshiki_paths import resolve_root, set_import_root


def argument_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Packwiz project TUI",
        add_help=add_help,
    )
    parser.add_argument(
        "--root",
        metavar="PATH",
        help="managed repository root (default: HUROSHIKI_ROOT, then current directory)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--pack",
        help="Open this MODPACK project immediately",
    )
    group.add_argument(
        "--template",
        help="Open this template project immediately",
    )
    return parser

# Resolve the managed repository before importing modules with root-derived globals.
_bootstrap_args, _ = argument_parser(add_help=False).parse_known_args(sys.argv[1:])
ROOT = resolve_root(_bootstrap_args.root)
set_import_root(ROOT)

try:
    from textual import events, on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container
    from textual.screen import ModalScreen, Screen
    from textual.timer import Timer
    from textual.widgets import DataTable, Input, Static, TextArea
except ModuleNotFoundError as error:
    if error.name == "textual":
        print(
            "huroshiki requires Textual. Enter the Nix development shell "
            "with `direnv allow` or `nix develop`."
        )
        raise SystemExit(1) from error
    raise

import huroshiki_core as core


def enabled_marker(enabled: bool) -> str:
    return "+" if enabled else "-"


def mod_side_marker(mod: core.ModInfo, enabled: bool) -> str:
    return "?" if mod.side_error is not None else enabled_marker(enabled)


class FilterInput(Input):
    BINDINGS = [
        Binding("q", "clear_screen_filter", "Clear filter", priority=True),
    ]

    def action_clear_screen_filter(self) -> None:
        screen = self.screen
        if not isinstance(screen, FilterListScreen) or not screen.clear_filter():
            self.insert_text_at_cursor("q")


class SideDataTable(DataTable):
    BINDINGS = [
        Binding("ctrl+c", "toggle_client_side", "Client side", priority=True),
        Binding("ctrl+s", "toggle_server_side", "Server side", priority=True),
    ]

    def action_toggle_client_side(self) -> None:
        self.screen.action_toggle_client_side()

    def action_toggle_server_side(self) -> None:
        self.screen.action_toggle_server_side()


class HuroshikiApp(App[None]):
    TITLE = "huroshiki"
    CSS_PATH = "huroshiki.tcss"
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, initial_project: str | None = None) -> None:
        super().__init__()
        self.initial_project = initial_project
        self.selected_project: str | None = None
        self.transactions: dict[str, core.PackTransaction] = {}

    def on_mount(self) -> None:
        if self.initial_project:
            project = core.project_info(self.initial_project)
            if project.error is not None:
                raise core.HuroshikiError(project.error)
            self.selected_project = self.initial_project
            self.push_screen(ProjectScreen(self.initial_project))
        else:
            self.push_screen(MainMenuScreen())

    def on_unmount(self) -> None:
        for transaction in self.transactions.values():
            transaction.discard()
        self.transactions.clear()

    def go_main(self) -> None:
        self.selected_project = None
        self.switch_screen(MainMenuScreen())

    def project_is_usable(self, project_key: str) -> bool:
        project = core.project_info(project_key)
        if project.error is None:
            return True
        self.notify(project.error, severity="error")
        return False

    def open_project(self, project_key: str) -> bool:
        if not self.project_is_usable(project_key):
            return False
        self.selected_project = project_key
        self.switch_screen(ProjectScreen(project_key))
        return True

    def open_install(self, project_key: str) -> None:
        if not self.project_is_usable(project_key):
            return
        self.selected_project = project_key
        self.switch_screen(InstallScreen(project_key))

    def open_list(self, project_key: str) -> None:
        if not self.project_is_usable(project_key):
            return
        self.selected_project = project_key
        self.switch_screen(InstalledModsScreen(project_key))

    def open_update(self, project_key: str) -> None:
        if not self.project_is_usable(project_key):
            return
        self.selected_project = project_key
        self.switch_screen(UpdateScreen(project_key))

    def open_settings(self, project_key: str) -> None:
        if not self.project_is_usable(project_key):
            return
        self.selected_project = project_key
        self.switch_screen(SettingsScreen(project_key))

    def open_deployment_settings(self, project_key: str) -> None:
        if not self.project_is_usable(project_key):
            return
        self.selected_project = project_key
        self.switch_screen(DeploymentSettingsScreen(project_key))

    def open_client_distribution_settings(self, project_key: str) -> None:
        if not self.project_is_usable(project_key):
            return
        self.selected_project = project_key
        self.switch_screen(ClientDistributionScreen(project_key))

    def open_templates(self, project_key: str) -> None:
        if not self.project_is_usable(project_key):
            return
        self.selected_project = project_key
        self.switch_screen(TemplateScreen(project_key))

    def open_template_editor(
        self,
        project_key: str,
        template: core.TemplateInfo,
    ) -> None:
        self.selected_project = project_key
        self.switch_screen(TemplateEditorScreen(project_key, template))

    def open_template_candidates(self, values: dict[str, str]) -> None:
        self.selected_project = None
        self.switch_screen(TemplateCandidateScreen(values))

    def open_state(self) -> None:
        self.selected_project = None
        self.switch_screen(StateScreen())

    def get_transaction(self, project_key: str) -> core.PackTransaction:
        transaction = self.transactions.get(project_key)
        if transaction is None or not transaction.active:
            transaction = core.PackTransaction.create(project_key)
            self.transactions[project_key] = transaction
        return transaction

    def remove_transaction(
        self,
        project_key: str,
        *,
        discard: bool = False,
    ) -> None:
        transaction = self.transactions.pop(project_key, None)
        if discard and transaction is not None:
            transaction.discard()


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("enter", "confirm", "Confirm"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, title: str, lines: Iterable[str]) -> None:
        super().__init__()
        self.dialog_title = title
        self.lines = list(lines)

    def compose(self) -> ComposeResult:
        text = "\n".join(self.lines)
        with Container(id="modal-dialog"):
            yield Static(self.dialog_title, classes="modal-title")
            yield Static(text, id="modal-message", markup=False)
            yield Static("Enter: confirm    Esc: cancel", classes="modal-help")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class MessageModal(ModalScreen[None]):
    BINDINGS = [
        Binding("enter", "close", "Close"),
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, title: str, lines: Iterable[str]) -> None:
        super().__init__()
        self.dialog_title = title
        self.lines = list(lines)

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog", classes="wide-dialog"):
            yield Static(self.dialog_title, classes="modal-title")
            yield Static("\n".join(self.lines), id="modal-message")
            yield Static("Enter / Esc: close", classes="modal-help")

    def action_close(self) -> None:
        self.dismiss(None)


class PublicPackUrlEditModal(ModalScreen[str | None]):
    BINDINGS = [
        Binding("ctrl+enter", "submit", "Review"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, value: str | None) -> None:
        super().__init__()
        self.value = value or ""

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog", classes="form-dialog"):
            yield Static("Edit Public Pack URL", classes="modal-title")
            yield Static("HTTPS URL ending in /pack.toml")
            yield Input(value=self.value, id="public-pack-url-input")
            yield Static(
                "Enter / Ctrl+Enter: review    Esc: cancel",
                classes="modal-help",
            )

    def on_mount(self) -> None:
        self.query_one("#public-pack-url-input", Input).focus()

    @on(Input.Submitted, "#public-pack-url-input")
    def submitted(self, _event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#public-pack-url-input", Input).value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewPackModal(ModalScreen[dict[str, str] | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Create"),
    ]

    FIELD_IDS = (
        "new-project-kind",
        "new-project-id",
        "new-display-name",
        "new-minecraft",
        "new-loader",
        "new-loader-version",
    )

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog", classes="form-dialog"):
            yield Static("Create project", classes="modal-title")
            yield Static("Type")
            yield Input(
                value="pack",
                placeholder="pack / template",
                id="new-project-kind",
            )
            yield Static("Project ID")
            yield Input(placeholder="industrial-base", id="new-project-id")
            yield Static("Display name")
            yield Input(placeholder="Industrial Base", id="new-display-name")
            yield Static("Minecraft version")
            yield Input(placeholder="1.21.1", id="new-minecraft")
            yield Static("Loader")
            yield Input(
                placeholder="neoforge / forge / fabric / quilt",
                id="new-loader",
            )
            yield Static("Loader version")
            yield Input(placeholder="21.1.234", id="new-loader-version")
            yield Static(
                "Tab: next field    Enter on last field / "
                "Ctrl+Enter: create    Esc: cancel",
                classes="modal-help",
            )

    def on_mount(self) -> None:
        self.query_one("#new-project-kind", Input).focus()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        inputs = [
            self.query_one(f"#{field_id}", Input)
            for field_id in self.FIELD_IDS
        ]
        current = inputs.index(event.input)
        if current < len(inputs) - 1:
            inputs[current + 1].focus()
            return
        self.action_submit()

    def action_submit(self) -> None:
        values = {
            field_id: self.query_one(f"#{field_id}", Input).value.strip()
            for field_id in self.FIELD_IDS
        }
        if not all(values.values()):
            self.app.notify("All fields are required", severity="error")
            return
        kind = values["new-project-kind"].lower()
        if kind not in core.PROJECT_KINDS:
            self.app.notify(
                "Type must be pack or template",
                severity="error",
            )
            return
        self.dismiss(
            {
                "kind": kind,
                "project_id": values["new-project-id"],
                "display_name": values["new-display-name"],
                "minecraft": values["new-minecraft"],
                "loader": values["new-loader"].lower(),
                "loader_version": values["new-loader-version"],
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class CreateFromTemplateModal(ModalScreen[dict[str, str] | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Continue"),
    ]

    FIELD_IDS = (
        "template-pack-id",
        "template-pack-name",
        "template-minecraft",
        "template-loader",
        "template-loader-version",
    )

    def __init__(self, template: core.ProjectInfo | None = None) -> None:
        super().__init__()
        self.template = template

    def compose(self) -> ComposeResult:
        minecraft = self.template.minecraft if self.template else ""
        loader = self.template.loader if self.template else ""
        loader_version = self.template.loader_version if self.template else ""
        with Container(id="modal-dialog", classes="form-dialog"):
            title = (
                f"Create MODPACK from {self.template.display_name}"
                if self.template
                else "Create MODPACK from template"
            )
            yield Static(title, classes="modal-title")
            yield Static("Project ID")
            yield Input(placeholder="industrial-pack", id="template-pack-id")
            yield Static("Display name")
            yield Input(placeholder="Industrial Pack", id="template-pack-name")
            yield Static("Minecraft version")
            yield Input(value=minecraft, placeholder="1.21.1", id="template-minecraft")
            yield Static("Loader")
            yield Input(value=loader, placeholder="neoforge", id="template-loader")
            yield Static("Loader version")
            yield Input(
                value=loader_version,
                placeholder="21.1.234",
                id="template-loader-version",
            )
            yield Static(
                "Templates are filtered by Minecraft version and loader only. "
                "A different loader version is allowed.",
                classes="modal-help",
            )

    def on_mount(self) -> None:
        self.query_one("#template-pack-id", Input).focus()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        inputs = [self.query_one(f"#{item}", Input) for item in self.FIELD_IDS]
        current = inputs.index(event.input)
        if current < len(inputs) - 1:
            inputs[current + 1].focus()
            return
        self.action_submit()

    def action_submit(self) -> None:
        values = {
            field_id: self.query_one(f"#{field_id}", Input).value.strip()
            for field_id in self.FIELD_IDS
        }
        if not all(values.values()):
            self.app.notify("All fields are required", severity="error")
            return
        self.dismiss(
            {
                "project_id": values["template-pack-id"],
                "display_name": values["template-pack-name"],
                "minecraft": values["template-minecraft"],
                "loader": values["template-loader"].lower(),
                "loader_version": values["template-loader-version"],
                "template_id": self.template.project_id if self.template else "",
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewTemplateModal(ModalScreen[dict[str, str] | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+enter", "submit", "Create"),
    ]

    FIELD_IDS = (
        "new-template-target",
        "new-template-path",
    )

    def compose(self) -> ComposeResult:
        with Container(id="modal-dialog", classes="form-dialog"):
            yield Static("Create project file", classes="modal-title")
            yield Static("Target")
            yield Input(
                value="common",
                placeholder="common / client / server",
                id="new-template-target",
            )
            yield Static("Relative path")
            yield Input(
                placeholder="config/example.toml",
                id="new-template-path",
            )
            yield Static(
                "Tab: next field    Enter on last field / "
                "Ctrl+Enter: create    Esc: cancel",
                classes="modal-help",
            )

    def on_mount(self) -> None:
        self.query_one("#new-template-target", Input).focus()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        inputs = [
            self.query_one(f"#{field_id}", Input)
            for field_id in self.FIELD_IDS
        ]
        current = inputs.index(event.input)
        if current < len(inputs) - 1:
            inputs[current + 1].focus()
            return
        self.action_submit()

    def action_submit(self) -> None:
        values = {
            field_id: self.query_one(f"#{field_id}", Input).value.strip()
            for field_id in self.FIELD_IDS
        }
        if not all(values.values()):
            self.app.notify("All fields are required", severity="error")
            return
        self.dismiss(
            {
                "target": values["new-template-target"],
                "relative_path": values["new-template-path"],
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class BaseScreen(Screen[None]):
    screen_title = "huroshiki"
    help_text = ""

    def compose_header(self) -> ComposeResult:
        yield Static(self.screen_title, id="screen-title")

    def compose_footer(self) -> ComposeResult:
        yield Static(self.help_text, id="key-help")

    @staticmethod
    def current_index(table: DataTable, length: int) -> int | None:
        if length <= 0:
            return None
        return max(0, min(table.cursor_row, length - 1))

    @staticmethod
    def move_table(table: DataTable, length: int, delta: int) -> None:
        if length <= 0:
            return
        row = (max(0, min(table.cursor_row, length - 1)) + delta) % length
        table.move_cursor(row=row)


class FilterListScreen(BaseScreen):
    BINDINGS = [
        Binding("q", "clear_filter_or_fallback", "Clear filter", priority=True),
    ]
    filter_input_id = ""
    filter_table_id = ""

    def reload_filter_rows(self, query: str) -> None:
        raise NotImplementedError

    def filter_row_count(self) -> int:
        raise NotImplementedError

    def filter_fallback(self) -> None:
        pass

    def clear_filter(self) -> bool:
        search = self.query_one(f"#{self.filter_input_id}", Input)
        if not search.value:
            return False
        table = self.query_one(f"#{self.filter_table_id}", DataTable)
        cursor_row = table.cursor_row
        search.value = ""
        self.reload_filter_rows("")
        row_count = self.filter_row_count()
        if row_count:
            table.move_cursor(row=max(0, min(cursor_row, row_count - 1)))
        table.focus()
        return True

    def action_clear_filter_or_fallback(self) -> None:
        if not self.clear_filter():
            self.filter_fallback()


class ProjectChildScreen:
    project_key: str
    recovery_parent_main: bool = False

    def return_to_project(self) -> None:
        if self.recovery_parent_main:
            self.app.go_main()
        elif not self.app.open_project(self.project_key):
            self.app.go_main()

    def return_to_project_files(self) -> None:
        self.app.switch_screen(
            TemplateScreen(
                self.project_key,
                recovery_parent_main=self.recovery_parent_main,
            )
        )


class MainMenuScreen(FilterListScreen):
    BINDINGS = FilterListScreen.BINDINGS
    screen_title = "huroshiki / Projects"
    help_text = (
        "Tab: focus  Enter: search/open  j/k: move  p: project  "
        "n: new  f: from template  d: delete  r: reload  x: state  q: clear/quit"
    )
    filter_input_id = "pack-search"
    filter_table_id = "pack-table"

    def __init__(self) -> None:
        super().__init__()
        self.all_projects: list[core.ProjectInfo] = []
        self.visible_projects: list[core.ProjectInfo] = []

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield FilterInput(placeholder="Search projects", id="pack-search")
        yield DataTable(id="pack-table")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#pack-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(
            "Type",
            "Project",
            "ID",
            "Minecraft",
            "Loader",
            "MODs",
            "Enabled",
        )
        self.reload_projects()
        table.focus()

    def reload_projects(self, query: str = "") -> None:
        try:
            self.all_projects = core.list_projects()
            self.visible_projects = core.filter_projects(
                self.all_projects,
                query,
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")
            self.all_projects = []
            self.visible_projects = []

        table = self.query_one("#pack-table", DataTable)
        table.clear()
        for project in self.visible_projects:
            loader = project.loader
            if project.loader_version:
                loader = f"{loader} {project.loader_version}"
            table.add_row(
                project.type_label,
                project.display_name,
                project.project_id,
                project.minecraft,
                loader,
                str(project.mod_count) if project.mod_count is not None else "-",
                "ERROR" if project.error else ("yes" if project.enabled else "no"),
            )

    def reload_filter_rows(self, query: str) -> None:
        self.reload_projects(query)

    def filter_row_count(self) -> int:
        return len(self.visible_projects)

    def filter_fallback(self) -> None:
        self.app.exit()

    @on(Input.Submitted, "#pack-search")
    def search(self, event: Input.Submitted) -> None:
        self.reload_projects(event.value)
        table = self.query_one("#pack-table", DataTable)
        if self.visible_projects:
            table.focus()

    def selected_project_info(self) -> core.ProjectInfo | None:
        table = self.query_one("#pack-table", DataTable)
        index = self.current_index(table, len(self.visible_projects))
        return None if index is None else self.visible_projects[index]

    def open_selected(self) -> None:
        project = self.selected_project_info()
        if project is None:
            self.app.notify("No project is selected", severity="warning")
            return
        if project.error is not None:
            self.show_project_error(project)
            return
        self.app.open_project(project.key)

    def show_project_error(self, project: core.ProjectInfo) -> None:
        self.app.push_screen(
            MessageModal(
                f"{project.type_label} ERROR",
                [
                    f"Project: {project.key}",
                    f"Path: {project.manifest_path}",
                    "",
                    project.error or "Unknown project loading error",
                    "",
                    "Repair the files externally, close this detail, then press r to reload.",
                    "For a broken MODPACK, close this detail and press t to inspect content files.",
                    "The project can also be deleted from the project list with d.",
                ],
            )
        )

    def request_delete(self) -> None:
        project = self.selected_project_info()
        if project is None:
            self.app.notify("No project is selected", severity="warning")
            return
        location = (
            f"packs/{project.project_id}"
            if project.kind == "pack"
            else f"templates/{project.project_id}"
        )
        self.app.push_screen(
            ConfirmModal(
                f"Delete {project.display_name}?",
                [
                    f"Type: {project.type_label}",
                    f"Local directory: {location}",
                    "The directory will move to .huroshiki/trash and can be restored.",
                ],
            ),
            lambda confirmed: self.delete_confirmed(project.key, confirmed),
        )

    def delete_confirmed(
        self,
        project_key: str,
        confirmed: bool | None,
    ) -> None:
        if not confirmed:
            return
        try:
            self.app.remove_transaction(project_key, discard=True)
            entry = core.delete_project(project_key)
            self.app.notify(f"Moved {project_key} to trash as {entry.name}")
            self.reload_projects(self.query_one("#pack-search", Input).value)
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def new_project(self) -> None:
        self.app.push_screen(NewPackModal(), self.create_project_from_modal)

    def new_from_template(self) -> None:
        self.app.push_screen(
            CreateFromTemplateModal(),
            self.open_template_candidates,
        )

    def open_template_candidates(
        self,
        values: dict[str, str] | None,
    ) -> None:
        if values is not None:
            self.app.open_template_candidates(values)

    def create_project_from_modal(
        self,
        values: dict[str, str] | None,
    ) -> None:
        if values is None:
            return
        with self.app.suspend():
            result = core.create_project(**values)
        if result == 0:
            self.app.notify(
                f"Created {values['kind']}:{values['project_id']}"
            )
            self.reload_projects()
        else:
            self.app.notify("Project creation failed", severity="error")

    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        table = self.query_one("#pack-table", DataTable)
        if isinstance(focused, Input):
            return
        if focused is table:
            if event.key == "j":
                self.move_table(table, len(self.visible_projects), 1)
            elif event.key == "k":
                self.move_table(table, len(self.visible_projects), -1)
            elif event.key in {"p", "enter"}:
                self.open_selected()
            elif event.key == "n":
                self.new_project()
            elif event.key == "f":
                self.new_from_template()
            elif event.key == "d":
                self.request_delete()
            elif event.key == "r":
                self.reload_projects(
                    self.query_one("#pack-search", Input).value
                )
            elif event.key == "x":
                self.app.open_state()
            elif event.key == "t":
                project = self.selected_project_info()
                if project is None or project.kind != "pack":
                    self.app.notify("Select a MODPACK to inspect files", severity="warning")
                else:
                    self.app.switch_screen(
                        TemplateScreen(project.key, recovery_parent_main=True)
                    )
            else:
                return
            event.stop()


class StateScreen(BaseScreen):
    screen_title = "huroshiki / State and Trash"
    help_text = (
        "j/k: move  Enter: restore  p: purge  c: dry-run cleanup  "
        "x: apply cleanup  Esc: main"
    )

    def __init__(self) -> None:
        super().__init__()
        self.items: list[core.StateItem] = []

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield DataTable(id="state-table")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#state-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Class", "Project", "Bytes", "State item")
        self.reload()
        table.focus()

    def reload(self) -> None:
        try:
            self.items = core.state_items()
        except Exception as error:
            self.items = []
            self.app.notify(str(error), severity="error")
        table = self.query_one("#state-table", DataTable)
        table.clear()
        for item in self.items:
            table.add_row(
                item.category,
                item.project_key or "-",
                str(item.bytes),
                str(item.path.relative_to(core.STATE_ROOT)),
            )

    def selected_item(self) -> core.StateItem | None:
        table = self.query_one("#state-table", DataTable)
        index = self.current_index(table, len(self.items))
        return None if index is None else self.items[index]

    def restore_selected(self) -> None:
        item = self.selected_item()
        if item is None or item.category != "trash":
            self.app.notify("Select a trash item to restore", severity="warning")
            return
        try:
            core.restore_trash(item.path.name)
            self.app.notify(f"Restored {item.project_key}")
            self.reload()
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def request_purge(self) -> None:
        item = self.selected_item()
        if item is None or item.category != "trash":
            self.app.notify("Select a trash item to purge", severity="warning")
            return
        self.app.push_screen(
            ConfirmModal(
                "Permanently purge trash item?",
                [
                    item.project_key or item.path.name,
                    f"Bytes: {item.bytes}",
                    "This cannot be undone.",
                ],
            ),
            lambda confirmed: self.purge_confirmed(item.path.name, confirmed),
        )

    def purge_confirmed(self, name: str, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            count, total = core.purge_trash(name)
            self.app.notify(f"Purged {count} item(s), {total} bytes")
            self.reload()
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def preview_cleanup(self) -> None:
        try:
            report = core.clean_state()
            total = sum(item.bytes for item in report.selected)
            self.app.push_screen(
                MessageModal(
                    "State cleanup dry run",
                    [
                        f"Would remove {len(report.selected)} item(s)",
                        f"Would free {total} bytes",
                    ],
                )
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def request_cleanup(self) -> None:
        try:
            report = core.clean_state()
        except Exception as error:
            self.app.notify(str(error), severity="error")
            return
        total = sum(item.bytes for item in report.selected)
        self.app.push_screen(
            ConfirmModal(
                "Apply state retention cleanup?",
                [
                    f"Remove {len(report.selected)} item(s)",
                    f"Free {total} bytes",
                    "Active transactions and locks are protected.",
                ],
            ),
            lambda confirmed: self.cleanup_confirmed(report.selected, confirmed),
        )

    def cleanup_confirmed(
        self,
        selected: tuple[core.StateItem, ...],
        confirmed: bool | None,
    ) -> None:
        if not confirmed:
            return
        try:
            report = core.clean_state(apply=True, expected=selected)
            self.app.notify(
                f"Removed {report.removed_count} item(s), "
                f"{report.removed_bytes} bytes"
            )
            self.reload()
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def on_key(self, event: events.Key) -> None:
        table = self.query_one("#state-table", DataTable)
        if event.key == "j":
            self.move_table(table, len(self.items), 1)
        elif event.key == "k":
            self.move_table(table, len(self.items), -1)
        elif event.key == "enter":
            self.restore_selected()
        elif event.key == "p":
            self.request_purge()
        elif event.key == "c":
            self.preview_cleanup()
        elif event.key == "x":
            self.request_cleanup()
        elif event.key == "escape":
            self.app.go_main()
        else:
            return
        event.stop()


class ProjectScreen(BaseScreen):
    help_text = "i: install  l: list  j/k: move  Enter: run  Esc: main"

    def __init__(self, project_key: str) -> None:
        super().__init__()
        self.project_key = project_key
        self.project = core.project_info(project_key)
        if self.project.error is not None:
            raise core.HuroshikiError(self.project.error)
        self.display_name = self.project.display_name
        self.screen_title = (
            f"{self.project.type_label} / {self.display_name}"
        )
        self.actions = core.project_actions(project_key)
        if self.project.kind == "pack":
            self.actions = (*self.actions, "settings")
            self.help_text = (
                "i: install  l: list  u: update  t: files  s: settings  "
                "j/k: move  Enter: run  Esc: main"
            )
        else:
            self.help_text = (
                "i: add MOD  l: MOD list  j/k: move  "
                "Enter: run  Esc: main"
            )

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Container(id="project-actions-container"):
            yield DataTable(id="project-actions")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#project-actions", DataTable)
        table.cursor_type = "row"
        table.show_header = False
        table.add_column("Action")
        for action in self.actions:
            table.add_row(action)
        table.focus()

    def run_selected(self) -> None:
        table = self.query_one("#project-actions", DataTable)
        index = self.current_index(table, len(self.actions))
        if index is None:
            return
        action = self.actions[index]
        if action == "settings":
            self.app.open_settings(self.project_key)
            return
        if action == "create MODPACK":
            self.app.push_screen(
                CreateFromTemplateModal(self.project),
                self.create_from_selected_template,
            )
            return
        if action in {"deploy", "publish"}:
            try:
                with self.app.suspend():
                    preview = core.prepare_deploy_preview(self.project_key, action)
            except Exception as error:
                self.app.notify(str(error), severity="error")
                return
            self.app.push_screen(
                ConfirmModal(
                    "Confirm deploy preview?",
                    preview.confirmation_lines,
                ),
                lambda confirmed: self.run_confirmed(action, preview, confirmed),
            )
            return
        try:
            confirmation = core.project_action_confirmation(
                self.project_key,
                action,
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")
            return
        if confirmation is not None:
            self.app.push_screen(
                ConfirmModal("Confirm remote action?", confirmation),
                lambda confirmed: self.run_confirmed(
                    action, confirmation, confirmed
                ),
            )
            return
        self.run_action(action)

    def run_confirmed(
        self,
        action: str,
        confirmation: tuple[str, ...] | core.ProjectDeployPreview,
        confirmed: bool | None,
    ) -> None:
        if confirmed:
            self.run_action(action, confirmation)
        elif isinstance(confirmation, core.ProjectDeployPreview):
            core.discard_deploy_preview(confirmation)

    def run_action(
        self,
        action: str,
        confirmation: tuple[str, ...] | core.ProjectDeployPreview | None = None,
    ) -> None:
        try:
            with self.app.suspend():
                result = core.run_project_action(
                    self.project_key, action, confirmation
                )
        except Exception as error:
            self.app.notify(str(error), severity="error")
            return
        if result == 0:
            self.app.notify(f"{action} completed")
        else:
            self.app.notify(f"{action} failed", severity="error")

    def create_from_selected_template(
        self,
        values: dict[str, str] | None,
    ) -> None:
        if values is None:
            return
        arguments = dict(values)
        arguments["template_ids"] = [arguments.pop("template_id")]
        try:
            composition = core.prepare_template_composition(
                template_ids=arguments["template_ids"],
                minecraft=arguments["minecraft"],
                loader=arguments["loader"],
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")
            return
        arguments["expected_composition"] = composition
        if composition.conflicts:
            self.app.push_screen(TemplateConflictScreen(arguments, composition))
            return
        self.finish_template_creation(arguments)

    def finish_template_creation(self, arguments: dict[str, object]) -> None:
        try:
            with self.app.suspend():
                report = core.create_pack_from_templates(**arguments)
            self.app.push_screen(
                MessageModal("Template creation result", report.warning_lines),
                lambda _: self.app.open_project(report.pack_key),
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def on_key(self, event: events.Key) -> None:
        table = self.query_one("#project-actions", DataTable)
        key = event.key
        if key == "j":
            self.move_table(table, len(self.actions), 1)
        elif key == "k":
            self.move_table(table, len(self.actions), -1)
        elif key == "enter":
            self.run_selected()
        elif key == "i":
            self.app.open_install(self.project_key)
        elif key == "l":
            self.app.open_list(self.project_key)
        elif key == "u":
            if self.project.kind == "pack":
                self.app.open_update(self.project_key)
            else:
                self.app.notify(
                    "Templates resolve compatible versions when creating a MODPACK",
                    severity="warning",
                )
        elif key == "t":
            if self.project.kind == "pack":
                self.app.open_templates(self.project_key)
            else:
                self.app.notify(
                    "Templates store MOD entries only",
                    severity="warning",
                )
        elif key == "s" and self.project.kind == "pack":
            self.app.open_settings(self.project_key)
        elif key == "escape":
            self.app.go_main()
        else:
            return
        event.stop()


class SettingsScreen(ProjectChildScreen, BaseScreen):
    screen_title = "Settings"
    help_text = "j/k: move  Enter: open  Esc: project"
    actions = ("Deployment", "Client Distribution")

    def __init__(self, project_key: str) -> None:
        super().__init__()
        self.project_key = project_key
        project = core.project_info(project_key)
        self.screen_title = f"{project.display_name} / Settings"

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Container(id="project-actions-container"):
            yield DataTable(id="settings-actions")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#settings-actions", DataTable)
        table.cursor_type = "row"
        table.show_header = False
        table.add_column("Settings")
        for action in self.actions:
            table.add_row(action)
        table.focus()

    def on_key(self, event: events.Key) -> None:
        table = self.query_one("#settings-actions", DataTable)
        if event.key == "j":
            self.move_table(table, len(self.actions), 1)
        elif event.key == "k":
            self.move_table(table, len(self.actions), -1)
        elif event.key == "enter":
            index = self.current_index(table, len(self.actions))
            if index is None:
                return
            if self.actions[index] == "Deployment":
                self.app.open_deployment_settings(self.project_key)
            elif self.actions[index] == "Client Distribution":
                self.app.open_client_distribution_settings(self.project_key)
        elif event.key == "escape":
            self.return_to_project()
        else:
            return
        event.stop()


class DeploymentSettingsScreen(BaseScreen):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("escape", "back", "Back", priority=True),
    ]
    FIELD_IDS = (
        "deployment-ssh-host",
        "deployment-stack-dir",
        "deployment-service",
        "deployment-rsync-host",
        "deployment-rsync-path",
    )
    help_text = "Tab: next field  Ctrl+S: review changes  Esc: settings"

    def __init__(self, project_key: str) -> None:
        super().__init__()
        self.project_key = project_key
        project = core.project_info(project_key)
        self.screen_title = f"{project.display_name} / Settings / Deployment"
        self.baseline = core.deployment_settings_baseline(project_key)
        self.settings = self.baseline.settings
        self.rsync_parts = core.split_rsync_target(self.settings.rsync_target)

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Container(id="deployment-settings-form"):
            yield Static("SSH host", classes="section-label")
            yield Input(value=self.settings.ssh_host, id="deployment-ssh-host")
            yield Static("Stack directory", classes="section-label")
            yield Input(value=self.settings.stack_dir, id="deployment-stack-dir")
            yield Static("Compose service", classes="section-label")
            yield Input(value=self.settings.service, id="deployment-service")
            yield Static("Rsync host", classes="section-label")
            yield Input(value=self.rsync_parts.host, id="deployment-rsync-host")
            yield Static("Rsync path", classes="section-label")
            yield Input(value=self.rsync_parts.path, id="deployment-rsync-path")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        self.query_one("#deployment-ssh-host", Input).focus()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        inputs = [self.query_one(f"#{field_id}", Input) for field_id in self.FIELD_IDS]
        index = inputs.index(event.input)
        if index < len(inputs) - 1:
            inputs[index + 1].focus()
        else:
            self.action_save()

    def proposed_settings(self) -> core.DeploymentSettings:
        return core.proposed_deployment_settings(
            ssh_host=self.query_one("#deployment-ssh-host", Input).value,
            stack_dir=self.query_one("#deployment-stack-dir", Input).value,
            service=self.query_one("#deployment-service", Input).value,
            rsync_host=self.query_one("#deployment-rsync-host", Input).value,
            rsync_path=self.query_one("#deployment-rsync-path", Input).value,
        )

    def action_save(self) -> None:
        try:
            proposed = self.proposed_settings()
        except Exception as error:
            self.app.notify(str(error), severity="error")
            return
        if proposed == self.settings:
            self.app.notify("Deployment settings are unchanged")
            return
        labels = (
            ("SSH host", self.settings.ssh_host, proposed.ssh_host),
            ("Stack directory", self.settings.stack_dir, proposed.stack_dir),
            ("Compose service", self.settings.service, proposed.service),
            ("Rsync target", self.settings.rsync_target, proposed.rsync_target),
        )
        lines = ["Save to: pack.local.yaml"]
        lines.extend(
            f"{label}: {before} -> {after}"
            for label, before, after in labels
            if before != after
        )
        self.app.push_screen(
            ConfirmModal("Save deployment settings?", lines),
            lambda confirmed: self.save_confirmed(proposed, confirmed),
        )

    def save_confirmed(
        self,
        proposed: core.DeploymentSettings,
        confirmed: bool | None,
    ) -> None:
        if not confirmed:
            return
        try:
            core.update_deployment_settings(
                self.project_key,
                proposed,
                expected_baseline=self.baseline,
            )
            self.baseline = core.deployment_settings_baseline(self.project_key)
            self.settings = self.baseline.settings
            self.rsync_parts = core.split_rsync_target(self.settings.rsync_target)
            values = (
                self.settings.ssh_host,
                self.settings.stack_dir,
                self.settings.service,
                self.rsync_parts.host,
                self.rsync_parts.path,
            )
            for field_id, value in zip(self.FIELD_IDS, values, strict=True):
                self.query_one(f"#{field_id}", Input).value = value
            self.app.notify("Deployment settings saved")
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def action_back(self) -> None:
        self.app.open_settings(self.project_key)


class ClientDistributionScreen(BaseScreen):
    BINDINGS = [
        Binding("e", "edit", "Edit", priority=True),
        Binding("c", "clear", "Clear local", priority=True),
        Binding("escape", "back", "Back", priority=True),
    ]
    help_text = "e: edit  c: clear local override  Esc: settings"

    def __init__(self, project_key: str) -> None:
        super().__init__()
        self.project_key = project_key
        project = core.project_info(project_key)
        self.screen_title = f"{project.display_name} / Settings / Client Distribution"
        self.baseline = core.public_pack_url_baseline(project_key)

    @property
    def info(self) -> core.PublicPackUrlInfo:
        return self.baseline.info

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Container(id="client-distribution-content"):
            yield Static(
                f"Source: {self.info.source}",
                id="public-pack-url-source",
                markup=False,
            )
            yield Static("Public Pack URL", classes="section-label")
            yield TextArea(
                self.info.value or "not configured",
                read_only=True,
                show_cursor=False,
                id="public-pack-url-display",
            )
            yield Static("Installer command", classes="section-label")
            yield TextArea(
                self.info.installer_command or "not configured",
                read_only=True,
                show_cursor=False,
                id="public-pack-command-display",
            )
        yield from self.compose_footer()

    def on_mount(self) -> None:
        self.query_one("#public-pack-url-display", TextArea).focus()

    def reload_info(self) -> None:
        self.baseline = core.public_pack_url_baseline(self.project_key)
        self.query_one("#public-pack-url-source", Static).update(
            f"Source: {self.info.source}"
        )
        self.query_one("#public-pack-url-display", TextArea).text = (
            self.info.value or "not configured"
        )
        self.query_one("#public-pack-command-display", TextArea).text = (
            self.info.installer_command or "not configured"
        )

    def action_edit(self) -> None:
        self.app.push_screen(
            PublicPackUrlEditModal(self.info.value),
            self.review_edit,
        )

    def review_edit(self, value: str | None) -> None:
        if value is None:
            return
        try:
            value = core.validate_public_pack_url(value)
        except Exception as error:
            self.app.notify(str(error), severity="error")
            return
        if value == self.info.value:
            self.app.notify("Public Pack URL is unchanged")
            return
        self.app.push_screen(
            ConfirmModal(
                "Save Public Pack URL?",
                (
                    "Save to: pack.local.yaml",
                    f"Old: {self.info.value or 'not configured'}",
                    f"New: {value}",
                ),
            ),
            lambda confirmed: self.save_edit(value, confirmed),
        )

    def save_edit(self, value: str, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            core.set_public_pack_url(
                self.project_key,
                value,
                expected_baseline=self.baseline,
            )
            self.reload_info()
            self.app.notify("Public Pack URL saved")
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def action_clear(self) -> None:
        if self.info.source != "local":
            self.app.notify("No local Public Pack URL override is configured")
            return
        fallback = self.baseline.committed_value or "not configured"
        self.app.push_screen(
            ConfirmModal(
                "Clear local Public Pack URL?",
                (
                    "Remove from: pack.local.yaml",
                    f"Old: {self.info.value}",
                    f"New: {fallback}",
                ),
            ),
            self.clear_confirmed,
        )

    def clear_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            core.clear_local_public_pack_url(
                self.project_key,
                expected_baseline=self.baseline,
            )
            self.reload_info()
            self.app.notify("Local Public Pack URL override cleared")
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def action_back(self) -> None:
        self.app.open_settings(self.project_key)


class TemplateEditorScreen(ProjectChildScreen, BaseScreen):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("escape", "back", "Back", priority=True),
    ]

    def __init__(
        self,
        project_key: str,
        template: core.TemplateInfo,
        *,
        recovery_parent_main: bool = False,
    ) -> None:
        super().__init__()
        self.project_key = project_key
        self.template = template
        self.recovery_parent_main = recovery_parent_main
        self.initial_text = core.read_template_text(
            project_key,
            template.target,
            template.relative_path,
        )
        project = core.project_info(project_key)
        self.screen_title = (
            f"{project.display_name} / Files / "
            f"{template.target}/{template.relative_path}"
        )
        self.help_text = "Ctrl+S: save  Esc: back"

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield TextArea(self.initial_text, id="template-editor")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        self.query_one("#template-editor", TextArea).focus()

    def current_text(self) -> str:
        return self.query_one("#template-editor", TextArea).text

    def action_save(self) -> None:
        try:
            text = self.current_text()
            core.write_template_text(
                self.project_key,
                self.template.target,
                self.template.relative_path,
                text,
            )
            self.initial_text = text
            self.app.notify(f"Saved {self.template.relative_path}")
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def action_back(self) -> None:
        if self.current_text() == self.initial_text:
            self.return_to_project_files()
            return
        self.app.push_screen(
            ConfirmModal(
                "Discard unsaved changes?",
                [
                    str(self.template.relative_path),
                    "The file has changes that have not been saved.",
                ],
            ),
            self.discard_confirmed,
        )

    def discard_confirmed(self, confirmed: bool | None) -> None:
        if confirmed:
            self.return_to_project_files()


class TemplateScreen(ProjectChildScreen, FilterListScreen):
    BINDINGS = FilterListScreen.BINDINGS
    help_text = (
        "Tab: focus  Enter/e: edit  j/k: move  n: new  "
        "d: delete  r: reload  q: clear filter  p: project  Esc: back"
    )
    filter_input_id = "template-search"
    filter_table_id = "template-table"

    def __init__(
        self,
        project_key: str,
        *,
        recovery_parent_main: bool = False,
    ) -> None:
        super().__init__()
        self.project_key = project_key
        self.recovery_parent_main = recovery_parent_main
        project = core.project_info(project_key)
        self.screen_title = f"{project.display_name} / Files"
        self.all_templates: list[core.TemplateInfo] = []
        self.visible_templates: list[core.TemplateInfo] = []

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield FilterInput(
            placeholder="Filter project files by target or path",
            id="template-search",
        )
        yield DataTable(id="template-table")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#template-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Target", "Path", "Bytes", "Status")
        self.reload_templates()
        table.focus()

    def reload_templates(self, query: str | None = None) -> None:
        if query is None:
            query = self.query_one("#template-search", Input).value
        try:
            self.all_templates = core.list_templates(self.project_key)
            self.visible_templates = core.filter_templates(
                self.all_templates,
                query,
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")
            self.all_templates = []
            self.visible_templates = []

        table = self.query_one("#template-table", DataTable)
        table.clear()
        for template in self.visible_templates:
            table.add_row(
                template.target,
                str(template.relative_path),
                str(template.size),
                template.error or "valid",
            )

    def reload_filter_rows(self, query: str) -> None:
        self.reload_templates(query)

    def filter_row_count(self) -> int:
        return len(self.visible_templates)

    @on(Input.Submitted, "#template-search")
    def filter_template_list(self, event: Input.Submitted) -> None:
        self.reload_templates(event.value)
        if self.visible_templates:
            self.query_one("#template-table", DataTable).focus()

    def current_template(self) -> core.TemplateInfo | None:
        table = self.query_one("#template-table", DataTable)
        index = self.current_index(table, len(self.visible_templates))
        if index is None:
            return None
        return self.visible_templates[index]

    def edit_current(self) -> None:
        template = self.current_template()
        if template is None:
            self.app.notify("No file is selected", severity="warning")
            return
        if template.error is not None:
            self.app.notify(template.error, severity="error")
            return
        try:
            self.app.switch_screen(
                TemplateEditorScreen(
                    self.project_key,
                    template,
                    recovery_parent_main=self.recovery_parent_main,
                )
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def new_template(self) -> None:
        self.app.push_screen(NewTemplateModal(), self.create_from_modal)

    def create_from_modal(
        self,
        values: dict[str, str] | None,
    ) -> None:
        if values is None:
            return
        try:
            template = core.create_template(
                self.project_key,
                values["target"],
                values["relative_path"],
            )
            self.reload_templates()
            self.app.switch_screen(
                TemplateEditorScreen(
                    self.project_key,
                    template,
                    recovery_parent_main=self.recovery_parent_main,
                )
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def request_delete(self) -> None:
        template = self.current_template()
        if template is None:
            self.app.notify("No file is selected", severity="warning")
            return
        if template.error is not None:
            self.app.notify(template.error, severity="error")
            return
        self.app.push_screen(
            ConfirmModal(
                "Delete project file?",
                [
                    f"Target: {template.target}",
                    f"Path: {template.relative_path}",
                    "This operation cannot be undone by huroshiki.",
                ],
            ),
            lambda confirmed: self.delete_confirmed(template, confirmed),
        )

    def delete_confirmed(
        self,
        template: core.TemplateInfo,
        confirmed: bool | None,
    ) -> None:
        if not confirmed:
            return
        try:
            core.delete_template(
                self.project_key,
                template.target,
                template.relative_path,
            )
            self.reload_templates()
            self.app.notify(f"Deleted {template.relative_path}")
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        table = self.query_one("#template-table", DataTable)
        if isinstance(focused, Input):
            if event.key == "escape":
                self.return_to_project()
                event.stop()
            return

        key = event.key
        if focused is table and key == "j":
            self.move_table(table, len(self.visible_templates), 1)
        elif focused is table and key == "k":
            self.move_table(table, len(self.visible_templates), -1)
        elif focused is table and key in {"enter", "e"}:
            self.edit_current()
        elif key == "n":
            self.new_template()
        elif key == "d":
            self.request_delete()
        elif key == "r":
            self.reload_templates()
        elif key == "p":
            self.return_to_project()
        elif key == "escape":
            self.return_to_project()
        else:
            return
        event.stop()


class TemplateCandidateScreen(BaseScreen):
    help_text = "j/k: move  Space: select  q: clear  Enter: create  Esc: main"

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__()
        self.values = values
        self.screen_title = "Create MODPACK / Select template"
        self.templates: list[core.ProjectInfo] = []
        self.selected_template_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield Static(
            "Candidates match Minecraft version and loader. "
            "The reference loader version is informational only.",
            id="template-apply-message",
        )
        yield Static("Selected: 0", id="template-selected-count")
        yield DataTable(id="template-candidate-table")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#template-candidate-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Selected", "Template", "ID", "Minecraft", "Loader", "Reference", "MODs")
        try:
            self.templates = core.compatible_templates(
                self.values["minecraft"],
                self.values["loader"],
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")
            self.templates = []
        self.reload_rows()
        if not self.templates:
            self.app.notify(
                "No template matches the selected Minecraft version and loader",
                severity="warning",
            )
        table.focus()

    def reload_rows(self) -> None:
        table = self.query_one("#template-candidate-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        for template in self.templates:
            table.add_row(
                "[x]" if template.project_id in self.selected_template_ids else "[ ]",
                template.display_name,
                template.project_id,
                template.minecraft,
                template.loader,
                template.loader_version,
                str(template.mod_count or 0),
            )
        self.query_one("#template-selected-count", Static).update(
            f"Selected: {len(self.selected_template_ids)}"
        )
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))

    def toggle_selected(self) -> None:
        table = self.query_one("#template-candidate-table", DataTable)
        index = self.current_index(table, len(self.templates))
        if index is None:
            return
        template_id = self.templates[index].project_id
        if template_id in self.selected_template_ids:
            self.selected_template_ids.remove(template_id)
        else:
            self.selected_template_ids.append(template_id)
        self.reload_rows()

    def create_selected(self) -> None:
        if not self.selected_template_ids:
            self.app.notify("Select at least one template", severity="warning")
            return
        arguments = dict(self.values)
        arguments["template_ids"] = list(self.selected_template_ids)
        try:
            composition = core.prepare_template_composition(
                template_ids=arguments["template_ids"],
                minecraft=arguments["minecraft"],
                loader=arguments["loader"],
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")
            return
        if composition.conflicts:
            arguments["expected_composition"] = composition
            self.app.push_screen(TemplateConflictScreen(arguments, composition))
            return
        arguments["expected_composition"] = composition
        self.finish_creation(arguments)

    def finish_creation(self, arguments: dict[str, object]) -> None:
        try:
            with self.app.suspend():
                report = core.create_pack_from_templates(**arguments)
            self.app.push_screen(
                MessageModal("Template creation result", report.warning_lines),
                lambda _: self.app.open_project(report.pack_key),
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def on_key(self, event: events.Key) -> None:
        table = self.query_one("#template-candidate-table", DataTable)
        if event.key == "j":
            self.move_table(table, len(self.templates), 1)
        elif event.key == "k":
            self.move_table(table, len(self.templates), -1)
        elif event.key == "space":
            self.toggle_selected()
        elif event.key == "q":
            self.selected_template_ids.clear()
            self.reload_rows()
        elif event.key == "enter":
            self.create_selected()
        elif event.key == "escape":
            self.app.go_main()
        else:
            return
        event.stop()


class TemplateConflictScreen(BaseScreen):
    help_text = "j/k: move  Space: toggle  Enter: create  Esc: templates"

    def __init__(
        self,
        values: dict[str, object],
        composition: core.TemplateComposition,
    ) -> None:
        super().__init__()
        self.values = values
        self.composition = composition
        self.screen_title = "Create MODPACK / Resolve conflicts"
        self.rows = [
            (conflict, candidate)
            for conflict in composition.conflicts
            for candidate in conflict.candidates
        ]
        self.selected: dict[str, list[str]] = {
            conflict.key: []
            for conflict in composition.conflicts
        }

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield Static("Each conflict must retain at least one source.", id="conflict-message")
        yield Static("", id="conflict-warning")
        yield DataTable(id="template-conflict-table")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#template-conflict-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(
            "Selected", "MOD", "Templates", "Provider", "Project ID", "URL", "Side"
        )
        self.reload_rows()
        table.focus()

    def reload_rows(self) -> None:
        table = self.query_one("#template-conflict-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        for conflict, candidate in self.rows:
            table.add_row(
                "[x]" if candidate.candidate_key in self.selected[conflict.key] else "[ ]",
                candidate.name,
                " -> ".join(candidate.template_ids),
                candidate.provider,
                candidate.project_id,
                candidate.url or "-",
                candidate.side,
            )
        multiple = [
            conflict.name
            for conflict in self.composition.conflicts
            if len(self.selected[conflict.key]) > 1
        ]
        warning = ""
        if multiple:
            warning = (
                "WARNING: multiple sources selected for " + ", ".join(multiple)
                + "; duplicate MOD IDs or functionality may prevent startup."
            )
        self.query_one("#conflict-warning", Static).update(warning)
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))

    def toggle_selected(self) -> None:
        table = self.query_one("#template-conflict-table", DataTable)
        index = self.current_index(table, len(self.rows))
        if index is None:
            return
        conflict, candidate = self.rows[index]
        selected = self.selected[conflict.key]
        if candidate.candidate_key in selected:
            if len(selected) == 1:
                self.app.notify(
                    f"{conflict.name} must retain at least one source",
                    severity="warning",
                )
                return
            selected.remove(candidate.candidate_key)
        else:
            proposed = [*selected, candidate.candidate_key]
            error = core.conflict_multi_selection_error(conflict, proposed)
            if error is not None:
                self.app.notify(error, severity="error")
                self.query_one("#conflict-warning", Static).update(error)
                return
            selected.append(candidate.candidate_key)
        self.reload_rows()

    def create_resolved(self, acknowledged: bool = False) -> None:
        unresolved = [
            conflict.name
            for conflict in self.composition.conflicts
            if not self.selected[conflict.key]
        ]
        if unresolved:
            self.app.notify(
                "Select at least one source for: " + ", ".join(unresolved),
                severity="warning",
            )
            return
        has_multiple = any(len(keys) > 1 for keys in self.selected.values())
        if has_multiple and not acknowledged:
            self.app.push_screen(
                ConfirmModal(
                    "Retain multiple MOD sources?",
                    [
                        "Duplicate MOD IDs or overlapping functionality may prevent startup.",
                        "Enter explicitly acknowledges this risk.",
                    ],
                ),
                lambda confirmed: self.create_resolved(True) if confirmed else None,
            )
            return
        resolutions = {
            conflict.key: core.ConflictResolution(
                tuple(
                    candidate.candidate_key
                    for candidate in conflict.candidates
                    if candidate.candidate_key in self.selected[conflict.key]
                ),
                acknowledge_duplicate_risk=has_multiple,
            )
            for conflict in self.composition.conflicts
        }
        arguments = dict(self.values)
        arguments["conflict_resolutions"] = resolutions
        try:
            with self.app.suspend():
                report = core.create_pack_from_templates(**arguments)
            self.app.push_screen(
                MessageModal("Template creation result", report.warning_lines),
                lambda _: self.app.open_project(report.pack_key),
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def on_key(self, event: events.Key) -> None:
        table = self.query_one("#template-conflict-table", DataTable)
        if event.key == "j":
            self.move_table(table, len(self.rows), 1)
        elif event.key == "k":
            self.move_table(table, len(self.rows), -1)
        elif event.key == "space":
            self.toggle_selected()
        elif event.key == "enter":
            self.create_resolved()
        elif event.key == "escape":
            self.app.pop_screen()
        else:
            return
        event.stop()


class InstallScreen(ProjectChildScreen, BaseScreen):
    BINDINGS = [
        Binding(
            "ctrl+t",
            "toggle_provider",
            "Provider",
            priority=True,
        ),
    ]

    help_text = (
        "Tab: focus  Ctrl+t: provider  Enter: search/select/review  "
        "q: discard results  j/k: move  Ctrl+c/Ctrl+s: toggle side  b: both  "
        "d: unstage  l: list  u: update  p: project  Esc: project"
    )

    def __init__(self, project_key: str) -> None:
        super().__init__()
        self.project_key = project_key
        config = core.project_config(project_key)
        self.display_name = str(config.get("display_name", core.split_project_key(project_key)[1]))
        self.screen_title = f"{self.display_name} / Install"
        self.provider = "modrinth"
        self.providers = ("modrinth", "curseforge", "url")
        self.default_client = True
        self.default_server = True
        self.search_results: list[core.InstallSearchResult] = []
        self.staged: list[core.ModInfo] = []
        self.operation: (
            core.ProviderSearchOperation
            | core.ResolvedAddOperation
            | core.PackwizAddOperation
            | None
        ) = None
        self.operation_thread: threading.Thread | None = None
        self.state = "idle"
        self._closing = False
        self._pending_navigation: Callable[[], None] | None = None
        self._pending_operation: object | None = None
        self._navigation_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield Static("Provider: Modrinth", id="provider-label")
        yield Input(
            placeholder="Search; use mr:<ID-or-slug> or a Modrinth URL for exact lookup",
            id="mod-search",
        )
        yield Static("Install side: C +  S +", id="install-side-label")
        yield Static("Enter a search term", id="packwiz-status")
        yield Static("Search results", classes="section-label")
        yield SideDataTable(id="search-results-table")
        yield Static("Staged changes", classes="section-label")
        yield SideDataTable(id="staged-table")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        results = self.query_one("#search-results-table", DataTable)
        results.cursor_type = "row"
        results.zebra_stripes = True
        results.add_columns("MOD", "Provider", "Project ID", "Details")

        staged = self.query_one("#staged-table", DataTable)
        staged.cursor_type = "row"
        staged.zebra_stripes = True
        staged.add_columns("MOD", "C", "S", "Source", "Metadata")

        self.refresh_staged()
        self.query_one("#mod-search", Input).focus()

    def on_unmount(self) -> None:
        self._closing = True
        if self.operation is not None and not self.operation.done.is_set():
            self.operation.cancel()

    def on_resize(self, event: events.Resize) -> None:
        if isinstance(self.operation, core.PackwizAddOperation) and not self.operation.done.is_set():
            self.operation.resize(event.size.width, event.size.height)

    def transaction(self) -> core.PackTransaction:
        return self.app.get_transaction(self.project_key)

    def refresh_staged(self) -> None:
        transaction = self.app.transactions.get(self.project_key)
        if transaction is None or not transaction.active:
            self.staged = []
        else:
            try:
                self.staged = transaction.staged_mods()
            except Exception as error:
                self.app.notify(str(error), severity="error")
                self.staged = []

        table = self.query_one("#staged-table", DataTable)
        table.clear()
        for mod in self.staged:
            table.add_row(
                mod.name,
                enabled_marker(mod.client),
                enabled_marker(mod.server),
                mod.provider,
                str(mod.relative_path),
            )

    def refresh_search_results(self) -> None:
        table = self.query_one("#search-results-table", DataTable)
        table.clear()
        for item in self.search_results:
            table.add_row(
                item.title,
                item.provider,
                item.project_id,
                item.subtitle,
            )

    def update_side_label(self) -> None:
        self.query_one("#install-side-label", Static).update(
            "Install side: "
            f"C {enabled_marker(self.default_client)}  "
            f"S {enabled_marker(self.default_server)}"
        )

    def set_status(self, message: str) -> None:
        self.query_one("#packwiz-status", Static).update(message)

    def toggle_provider(self) -> None:
        if self._pending_navigation is not None:
            return
        if self.operation is not None and not self.operation.done.is_set():
            self.app.notify("Wait for the active add operation", severity="warning")
            return
        index = self.providers.index(self.provider)
        self.provider = self.providers[(index + 1) % len(self.providers)]
        labels = {
            "modrinth": "Modrinth",
            "curseforge": "CurseForge",
            "url": "URL",
        }
        placeholders = {
            "modrinth": "Search; use mr:<ID-or-slug> or a Modrinth URL for exact lookup",
            "curseforge": "Numeric CurseForge project ID",
            "url": "Public URL of the self-hosted MOD JAR",
        }
        self.query_one("#provider-label", Static).update(
            f"Provider: {labels[self.provider]}"
        )
        self.query_one("#mod-search", Input).placeholder = placeholders[self.provider]
        if self.provider == "url":
            self.set_status(
                "Enter the same public .jar URL that Packwiz clients can download"
            )
        else:
            self.set_status("Enter a search term")

    def action_toggle_provider(self) -> None:
        self.toggle_provider()

    def action_toggle_client_side(self) -> None:
        if self._pending_navigation is not None:
            return
        focused = self.focused
        staged = self.query_one("#staged-table", DataTable)
        self.toggle_client(staged=focused is staged)

    def action_toggle_server_side(self) -> None:
        if self._pending_navigation is not None:
            return
        focused = self.focused
        staged = self.query_one("#staged-table", DataTable)
        self.toggle_server(staged=focused is staged)

    def cancel_operation(self) -> None:
        if self.operation is not None and not self.operation.done.is_set():
            self.operation.cancel()

    def navigate_after_cancellation(self, destination: Callable[[], None]) -> None:
        if self._pending_navigation is not None:
            return
        operation = self.operation
        if operation is None or operation.done.is_set():
            destination()
            return

        self._pending_navigation = destination
        self._pending_operation = operation
        self.set_status("Cancelling Packwiz operation before leaving...")
        try:
            operation.cancel()
        except Exception as error:
            self._pending_navigation = None
            self._pending_operation = None
            self.app.notify(str(error), severity="error")
            return
        self._navigation_timer = self.set_interval(
            0.05, self._complete_pending_navigation
        )

    def _complete_pending_navigation(self) -> None:
        destination = self._pending_navigation
        operation = self._pending_operation
        if destination is None or operation is None or not operation.done.is_set():
            return
        if self._navigation_timer is not None:
            self._navigation_timer.pause()
            self._navigation_timer = None
        self._pending_navigation = None
        self._pending_operation = None
        destination()

    def discard_search_results(self) -> None:
        if not self.search_results:
            return
        self.search_results = []
        self.refresh_search_results()
        self.state = "idle"
        self.set_status("Search results discarded")
        self.query_one("#mod-search", Input).focus()

    @on(Input.Submitted, "#mod-search")
    def start_search(self, event: Input.Submitted) -> None:
        if self._pending_navigation is not None:
            return
        query = event.value.strip()
        if not query:
            self.app.notify("Enter a search term", severity="warning")
            return
        if self.operation is not None and not self.operation.done.is_set():
            self.app.notify("An install operation is already running", severity="warning")
            return

        self.search_results = []
        self.refresh_search_results()
        lowered_query = query.lower()
        exact_modrinth_selector = lowered_query.startswith("mr:") or (
            "modrinth.com/" in lowered_query
        )
        try:
            normalized_provider, normalized_query = core.normalize_add_selector(
                self.provider, query
            )
        except Exception as error:
            self.set_status(str(error))
            self.app.notify(str(error), severity="error")
            return

        if normalized_provider == "curseforge" and not normalized_query.isdecimal():
            message = (
                "CurseForge search is unavailable. "
                "Enter a numeric CurseForge project ID."
            )
            self.set_status(message)
            self.app.notify(message, severity="warning")
            return

        event.input.disabled = True
        if normalized_provider == "modrinth" and not exact_modrinth_selector:
            minecraft, loader, _ = core.packctl.project_versions(
                self.transaction().source
            )
            operation = core.ProviderSearchOperation(
                provider="modrinth",
                query=normalized_query,
                minecraft=minecraft,
                loader=loader,
            )
            self.operation = operation
            self.state = "searching"
            self.set_status("Searching Modrinth...")
            target = self._run_search
        else:
            canonical_id = (
                normalized_query if normalized_provider == "curseforge" else None
            )
            try:
                self._start_resolved_operation(
                    provider=normalized_provider,
                    selector=normalized_query,
                    canonical_project_id=canonical_id,
                )
            except Exception as error:
                event.input.disabled = False
                self.state = "idle"
                self.set_status(str(error))
                self.app.notify(str(error), severity="error")
            return

        self.operation_thread = threading.Thread(
            target=target,
            args=(operation,),
            name=f"huroshiki-provider-search-{self.project_key}",
            daemon=True,
        )
        self.operation_thread.start()

    def _start_resolved_operation(
        self,
        *,
        provider: str,
        selector: str,
        canonical_project_id: str | None,
    ) -> None:
        side = core.side_from_flags(self.default_client, self.default_server)
        if provider == "url":
            operation: core.ResolvedAddOperation | core.PackwizAddOperation = (
                self.transaction().begin_add(
                    provider,
                    selector,
                    client=self.default_client,
                    server=self.default_server,
                )
            )
            status = "Downloading and staging the self-hosted MOD URL..."
        else:
            operation = self.transaction().begin_resolved_add(
                provider=provider,
                selector=selector,
                canonical_project_id=canonical_project_id,
                side=side,
            )
            label = canonical_project_id or selector
            status = f"Resolving {label} and dependencies..."
        self.operation = operation
        self.state = "resolving"
        self.set_status(status)
        self.query_one("#mod-search", Input).disabled = True
        self.operation_thread = threading.Thread(
            target=self._run_operation,
            args=(operation,),
            name=f"huroshiki-resolved-add-{self.project_key}",
            daemon=True,
        )
        self.operation_thread.start()

    def _run_search(self, operation: core.ProviderSearchOperation) -> None:
        operation.run()
        try:
            self.app.call_from_thread(self._search_finished, operation)
        except Exception:
            pass

    def _search_finished(self, operation: core.ProviderSearchOperation) -> None:
        if self.operation is not operation:
            return
        self.operation = None
        self.operation_thread = None
        if self._closing:
            return
        search = self.query_one("#mod-search", Input)
        search.disabled = False
        if operation.cancelled:
            self.state = "idle"
            self.set_status("Provider search cancelled")
            search.focus()
            return
        if operation.error is not None:
            self.state = "idle"
            self.set_status(operation.error)
            self.app.notify(operation.error, severity="warning")
            search.focus()
            return
        self.search_results = [
            core.InstallSearchResult(
                project.provider,
                project.project_id,
                project.title,
                " - ".join(
                    part for part in (project.author, project.description) if part
                ),
            )
            for project in operation.results
        ]
        self.refresh_search_results()
        self.state = "showing_results"
        if self.search_results:
            self.set_status(
                f"{len(self.search_results)} canonical result(s); select with j/k and Enter"
            )
            self.query_one("#search-results-table", DataTable).focus()
        else:
            self.set_status("Modrinth returned no matching projects")
            search.focus()

    def _run_operation(
        self, operation: core.ResolvedAddOperation | core.PackwizAddOperation
    ) -> None:
        result = operation.run()
        try:
            self.app.call_from_thread(self._operation_finished, operation, result)
        except Exception:
            # The app or this screen may already be shutting down.
            pass

    def _operation_finished(
        self,
        operation: core.ResolvedAddOperation | core.PackwizAddOperation,
        result: core.AddOperationResult,
    ) -> None:
        if self.operation is not operation:
            return
        self.operation = None
        self.operation_thread = None
        if self._closing:
            return

        search = self.query_one("#mod-search", Input)
        search.disabled = False
        self.state = "idle"
        self.search_results = []
        self.refresh_search_results()
        self.refresh_staged()

        if result.success:
            search.value = ""
            self.set_status(f"{result.message}. Log: {result.text_log}")
            self.app.notify(result.message)
        elif result.cancelled:
            self.set_status(f"Search cancelled. Log: {result.text_log}")
        else:
            self.set_status(f"{result.message}. Log: {result.text_log}")
            self.app.notify(result.message, severity="warning")
        search.focus()

    def current_search_result(self) -> core.InstallSearchResult | None:
        table = self.query_one("#search-results-table", DataTable)
        index = self.current_index(table, len(self.search_results))
        return None if index is None else self.search_results[index]

    def choose_search_result(self) -> None:
        item = self.current_search_result()
        if item is None:
            self.app.notify("No provider result is selected", severity="warning")
            return
        try:
            self._start_resolved_operation(
                provider=item.provider,
                selector=item.project_id,
                canonical_project_id=item.project_id,
            )
            self.set_status(f"Resolving {item.title} and dependencies...")
            self.search_results = []
            self.refresh_search_results()
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def current_staged_mod(self) -> core.ModInfo | None:
        table = self.query_one("#staged-table", DataTable)
        index = self.current_index(table, len(self.staged))
        return None if index is None else self.staged[index]

    def update_staged_side(self, client: bool, server: bool) -> None:
        mod = self.current_staged_mod()
        if mod is None:
            self.app.notify("No staged MOD is selected", severity="warning")
            return
        try:
            self.transaction().set_side(mod.relative_path, client, server)
            self.refresh_staged()
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def remove_staged_mod(self) -> None:
        mod = self.current_staged_mod()
        if mod is None:
            self.app.notify("No staged MOD is selected", severity="warning")
            return
        try:
            self.transaction().unstage(mod.relative_path)
            self.refresh_staged()
            self.set_status(f"Removed {mod.name} from staged changes")
            self.app.notify(f"Unstaged {mod.name}")
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def toggle_client(self, *, staged: bool) -> None:
        if staged:
            mod = self.current_staged_mod()
            if mod is None:
                return
            client = not mod.client
            if not client and not mod.server:
                self.app.notify(
                    "At least one side must remain enabled", severity="warning"
                )
                return
            self.update_staged_side(client, mod.server)
            return

        client = not self.default_client
        if not client and not self.default_server:
            self.app.notify("At least one side must remain enabled", severity="warning")
            return
        self.default_client = client
        self.update_side_label()

    def toggle_server(self, *, staged: bool) -> None:
        if staged:
            mod = self.current_staged_mod()
            if mod is None:
                return
            server = not mod.server
            if not server and not mod.client:
                self.app.notify(
                    "At least one side must remain enabled", severity="warning"
                )
                return
            self.update_staged_side(mod.client, server)
            return

        server = not self.default_server
        if not server and not self.default_client:
            self.app.notify("At least one side must remain enabled", severity="warning")
            return
        self.default_server = server
        self.update_side_label()

    def enable_both(self, *, staged: bool) -> None:
        if staged:
            self.update_staged_side(True, True)
        else:
            self.default_client = True
            self.default_server = True
            self.update_side_label()

    def review(self) -> None:
        if self.operation is not None and not self.operation.done.is_set():
            self.app.notify("Wait for the install operation to finish", severity="warning")
            return
        self.refresh_staged()
        if not self.staged:
            self.app.notify("No staged changes", severity="warning")
            return
        lines = [
            f"{mod.name}  C={'yes' if mod.client else 'no'}  "
            f"S={'yes' if mod.server else 'no'}  ({mod.provider})"
            for mod in self.staged
        ]
        self.app.push_screen(
            ConfirmModal(
                f"Apply {len(self.staged)} staged MOD changes?",
                [
                    *lines,
                    "",
                    "Packwiz refresh runs before an atomic source-directory switch.",
                ],
            ),
            self.apply_confirmed,
        )

    def apply_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        transaction = self.app.transactions.get(self.project_key)
        if transaction is None:
            return
        try:
            with self.app.suspend():
                transaction.apply()
            self.app.transactions.pop(self.project_key, None)
            self.refresh_staged()
            self.app.notify("Transaction applied")
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def on_key(self, event: events.Key) -> None:
        if self._pending_navigation is not None:
            event.stop()
            return
        focused = self.focused
        results = self.query_one("#search-results-table", DataTable)
        staged = self.query_one("#staged-table", DataTable)

        if isinstance(focused, Input):
            if event.key == "escape":
                self.navigate_after_cancellation(self.return_to_project)
                event.stop()
            return

        key = event.key
        if key == "q" and self.search_results:
            self.discard_search_results()
        elif focused is results and key == "j":
            self.move_table(results, len(self.search_results), 1)
        elif focused is results and key == "k":
            self.move_table(results, len(self.search_results), -1)
        elif focused is results and key == "enter":
            self.choose_search_result()
        elif focused is staged and key == "j":
            self.move_table(staged, len(self.staged), 1)
        elif focused is staged and key == "k":
            self.move_table(staged, len(self.staged), -1)
        elif key == "b":
            self.enable_both(staged=focused is staged)
        elif focused is staged and key == "d":
            self.remove_staged_mod()
        elif focused is staged and key == "enter":
            self.review()
        elif key == "l":
            self.navigate_after_cancellation(lambda: self.app.open_list(self.project_key))
        elif key == "u":
            if core.split_project_key(self.project_key)[0] == "pack":
                self.navigate_after_cancellation(
                    lambda: self.app.open_update(self.project_key)
                )
            else:
                self.app.notify(
                    "Templates resolve compatible versions during MODPACK creation",
                    severity="warning",
                )
        elif key == "p":
            self.navigate_after_cancellation(self.return_to_project)
        elif key == "escape":
            self.navigate_after_cancellation(self.return_to_project)
        else:
            return
        event.stop()


class InstalledModsScreen(ProjectChildScreen, FilterListScreen):
    BINDINGS = FilterListScreen.BINDINGS
    help_text = (
        "Tab: focus  Enter: filter  j/k: move  Space: select  Ctrl+c/Ctrl+s: toggle side  "
        "b: both  d: delete  q: clear filter  i: install  u: update  "
        "m: help  p: project  Esc: project"
    )
    filter_input_id = "installed-search"
    filter_table_id = "installed-table"

    def __init__(self, project_key: str) -> None:
        super().__init__()
        self.project_key = project_key
        config = core.project_config(project_key)
        self.display_name = str(config.get("display_name", core.split_project_key(project_key)[1]))
        self.screen_title = f"{self.display_name} / Installed MODs"
        self.all_mods: list[core.ModInfo] = []
        self.visible_mods: list[core.ModInfo] = []
        self.selected_paths: set[Path] = set()

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield FilterInput(placeholder="Filter installed MODs", id="installed-search")
        yield SideDataTable(id="installed-table")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#installed-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Del", "MOD", "C", "S", "Source", "Metadata")
        self.reload_mods()
        table.focus()

    def reload_mods(self, query: str | None = None) -> None:
        if query is None:
            query = self.query_one("#installed-search", Input).value
        try:
            self.all_mods = core.list_mods(self.project_key)
            for mod in self.all_mods:
                mod.selected = mod.relative_path in self.selected_paths
            self.visible_mods = core.filter_mods(self.all_mods, query)
        except Exception as error:
            self.app.notify(str(error), severity="error")
            self.all_mods = []
            self.visible_mods = []

        table = self.query_one("#installed-table", DataTable)
        table.clear()
        for mod in self.visible_mods:
            table.add_row(
                "[*]" if mod.selected else "[ ]",
                mod.name,
                mod_side_marker(mod, mod.client),
                mod_side_marker(mod, mod.server),
                mod.provider,
                str(mod.relative_path),
            )

    def reload_filter_rows(self, query: str) -> None:
        self.reload_mods(query)

    def filter_row_count(self) -> int:
        return len(self.visible_mods)

    @on(Input.Submitted, "#installed-search")
    def filter_installed(self, event: Input.Submitted) -> None:
        self.reload_mods(event.value)
        if self.visible_mods:
            self.query_one("#installed-table", DataTable).focus()

    def current_mod(self) -> core.ModInfo | None:
        table = self.query_one("#installed-table", DataTable)
        index = self.current_index(table, len(self.visible_mods))
        return None if index is None else self.visible_mods[index]

    def toggle_selected(self) -> None:
        mod = self.current_mod()
        if mod is None:
            return
        if mod.relative_path in self.selected_paths:
            self.selected_paths.remove(mod.relative_path)
        else:
            self.selected_paths.add(mod.relative_path)
        self.reload_mods()

    def set_side(self, client: bool, server: bool) -> None:
        mod = self.current_mod()
        if mod is None:
            return
        try:
            core.set_installed_mod_side(
                self.project_key,
                mod.relative_path,
                client,
                server,
            )
            self.reload_mods()
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def toggle_client(self) -> None:
        mod = self.current_mod()
        if mod is None:
            return
        client = not mod.client
        if not client and not mod.server:
            self.app.notify("At least one side must remain enabled", severity="warning")
            return
        self.set_side(client, mod.server)

    def toggle_server(self) -> None:
        mod = self.current_mod()
        if mod is None:
            return
        server = not mod.server
        if not server and not mod.client:
            self.app.notify("At least one side must remain enabled", severity="warning")
            return
        self.set_side(mod.client, server)

    def action_toggle_client_side(self) -> None:
        if self.focused is self.query_one("#installed-table", DataTable):
            self.toggle_client()

    def action_toggle_server_side(self) -> None:
        if self.focused is self.query_one("#installed-table", DataTable):
            self.toggle_server()

    def request_delete(self) -> None:
        selected = [
            mod for mod in self.all_mods if mod.relative_path in self.selected_paths
        ]
        if not selected:
            self.app.notify("Select MODs with Space first", severity="warning")
            return
        self.app.push_screen(
            ConfirmModal(
                f"Delete {len(selected)} MODs?",
                [
                    *(mod.name for mod in selected),
                    "",
                    "All removals and the refreshed index will be applied atomically.",
                ],
            ),
            lambda confirmed: self.delete_confirmed(selected, confirmed),
        )

    def delete_confirmed(
        self,
        selected: list[core.ModInfo],
        confirmed: bool | None,
    ) -> None:
        if not confirmed:
            return
        try:
            with self.app.suspend():
                result = core.remove_installed_mods(
                    self.project_key,
                    [
                        str(mod.relative_path)
                        if core.split_project_key(self.project_key)[0] == "template"
                        else mod.slug
                        for mod in selected
                    ],
                )
        except Exception as error:
            self.reload_mods()
            self.app.notify(str(error), severity="error")
            return
        if result == 0:
            self.selected_paths.clear()
            self.reload_mods()
            self.app.notify("Selected MODs deleted")
        else:
            self.reload_mods()
            self.app.notify("MOD deletion stopped after an error", severity="error")

    def show_help(self) -> None:
        self.app.push_screen(
            MessageModal(
                "Installed MODs",
                [
                    "Space toggles the deletion mark.",
                    "Ctrl+C and Ctrl+S toggle client/server deployment for the highlighted MOD.",
                    "b enables both client and server.",
                    "At least one side must remain enabled.",
                    "d reviews and deletes all marked MODs.",
                ],
            )
        )

    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        table = self.query_one("#installed-table", DataTable)
        if isinstance(focused, Input):
            if event.key == "escape":
                self.return_to_project()
                event.stop()
            return

        key = event.key
        if focused is table and key == "j":
            self.move_table(table, len(self.visible_mods), 1)
        elif focused is table and key == "k":
            self.move_table(table, len(self.visible_mods), -1)
        elif focused is table and key == "space":
            self.toggle_selected()
        elif focused is table and key == "b":
            self.set_side(True, True)
        elif key == "d":
            self.request_delete()
        elif key == "i":
            self.app.open_install(self.project_key)
        elif key == "u":
            if core.split_project_key(self.project_key)[0] == "pack":
                self.app.open_update(self.project_key)
            else:
                self.app.notify(
                    "Templates resolve compatible versions during MODPACK creation",
                    severity="warning",
                )
        elif key == "m":
            self.show_help()
        elif key == "p":
            self.return_to_project()
        elif key == "escape":
            self.return_to_project()
        else:
            return
        event.stop()


class UpdateScreen(ProjectChildScreen, BaseScreen):
    help_text = "j/k: move  Space: toggle  Enter: apply  i: install  l: list  Esc: project"

    def __init__(self, project_key: str) -> None:
        super().__init__()
        self.project_key = project_key
        config = core.project_config(project_key)
        self.display_name = str(config.get("display_name", core.split_project_key(project_key)[1]))
        self.screen_title = f"{self.display_name} / Update"
        self.transaction: core.PackTransaction | None = None
        self.operation: core.UpdatePreparationOperation | None = None
        self.candidates: list[core.UpdateCandidate] = []
        self.selected_paths: set[Path] = set()
        self.operation_thread: threading.Thread | None = None
        self.operation_timer: Timer | None = None
        self.leave_after_cancel = False

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield Static(
            "Updates are staged on a transaction copy. Toggle candidates before applying.",
            id="update-message",
        )
        yield DataTable(id="update-options")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#update-options", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(
            "Use", "MOD", "Provider", "Current", "New", "Files", "Status"
        )
        try:
            self.operation = core.UpdatePreparationOperation(self.project_key)
            self.query_one("#update-message", Static).update("Preparing updates...")
            self.operation_thread = threading.Thread(
                target=self.operation.run,
                name=f"huroshiki-update-{self.project_key}",
                daemon=False,
            )
            self.operation_thread.start()
            self.operation_timer = self.set_interval(0.05, self._poll_preparation)
        except Exception as error:
            if self.operation is not None:
                self.operation.cancel()
            self.app.notify(str(error), severity="error")
        table.focus()

    def _show_progress(self, progress: core.UpdateProgress) -> None:
        if progress.phase == "normalizing":
            message = f"Preparing updates: {progress.completed} / {progress.total} (normalizing)"
        elif progress.phase == "resolving":
            message = (
                f"Preparing updates: {progress.completed} / {progress.total}\n"
                f"Current: {progress.mod_name} [{progress.provider}]"
            )
        elif progress.phase == "cancelled":
            message = "Cancelling update preparation..."
        else:
            message = progress.message or (
                f"Preparing updates: {progress.completed} / {progress.total}"
            )
        self.query_one("#update-message", Static).update(message)

    def _poll_preparation(self) -> None:
        operation = self.operation
        if operation is None:
            return
        for progress in operation.drain_progress():
            self._show_progress(progress)
        if not operation.done.is_set():
            return
        if self.operation_timer is not None:
            self.operation_timer.pause()
            self.operation_timer = None
        self.operation_thread = None
        self.operation = None
        if operation.error is not None:
            self.query_one("#update-message", Static).update(str(operation.error))
            self.app.notify(str(operation.error), severity="error")
            return
        if operation.cancelled:
            if self.leave_after_cancel:
                self.return_to_project()
            return
        self.transaction = operation.claim_transaction()
        self.candidates = list(operation.candidates)
        self.selected_paths = {
            candidate.relative_path
            for candidate in self.candidates
            if candidate.available
        }
        self.reload_candidates()
        self.query_one("#update-message", Static).update(
            "Updates prepared. Toggle candidates before applying."
        )
        if not self.selected_paths:
            self.app.notify("No MOD updates are available")

    def reload_candidates(self) -> None:
        table = self.query_one("#update-options", DataTable)
        table.clear()
        for candidate in self.candidates:
            table.add_row(
                "[*]" if candidate.relative_path in self.selected_paths else "[ ]",
                candidate.name,
                candidate.provider,
                candidate.current_version,
                candidate.new_version,
                str(candidate.file_count) if candidate.available else "-",
                (
                    f"unavailable: {candidate.error}"
                    if candidate.error
                    else candidate.status
                ),
            )

    def toggle_candidate(self) -> None:
        if self.operation is not None:
            self.app.notify("Update preparation is still running", severity="warning")
            return
        table = self.query_one("#update-options", DataTable)
        index = self.current_index(table, len(self.candidates))
        if index is None:
            return
        candidate = self.candidates[index]
        if not candidate.available:
            self.app.notify(
                f"{candidate.name} is {candidate.status} and cannot be selected",
                severity="warning",
            )
            return
        if candidate.relative_path in self.selected_paths:
            self.selected_paths.remove(candidate.relative_path)
        else:
            self.selected_paths.add(candidate.relative_path)
        self.reload_candidates()

    def request_update(self) -> None:
        if self.operation is not None:
            self.app.notify("Update preparation is still running", severity="warning")
            return
        selected = [
            candidate
            for candidate in self.candidates
            if candidate.relative_path in self.selected_paths
        ]
        if not selected:
            self.app.notify("Select at least one available update", severity="warning")
            return
        self.app.push_screen(
            ConfirmModal(
                f"Apply {len(selected)} MOD update(s)?",
                [
                    *(
                        f"{item.name} [{item.provider}] "
                        f"{item.current_version} -> {item.new_version}"
                        for item in selected
                    ),
                    "",
                    f"Selected closures contain {sum(item.file_count for item in selected)} "
                    f"file change(s), including "
                    f"{sum(item.added_dependencies for item in selected)} added "
                    "dependency record(s).",
                    "The real source will change only if every step succeeds.",
                ],
            ),
            self.update_confirmed,
        )

    def update_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        if self.transaction is None:
            return
        try:
            self.transaction.select_updates(self.selected_paths)
            with self.app.suspend():
                self.transaction.apply()
            self.app.notify(f"Applied {len(self.selected_paths)} MOD update(s)")
            self.transaction = None
            self.app.open_list(self.project_key)
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def discard_and_leave(self) -> None:
        if self.operation is not None:
            self.leave_after_cancel = True
            self.operation.cancel()
            self.query_one("#update-message", Static).update(
                "Cancelling update preparation..."
            )
            return
        if self.transaction is not None:
            self.transaction.discard()
            self.transaction = None
        self.return_to_project()

    def discard_and_navigate(self, destination: Callable[[], None]) -> None:
        if self.transaction is not None:
            self.transaction.discard()
            self.transaction = None
        destination()

    def on_unmount(self) -> None:
        if self.operation_timer is not None:
            self.operation_timer.pause()
            self.operation_timer = None
        if self.operation is not None:
            self.operation.cancel()

    def on_key(self, event: events.Key) -> None:
        table = self.query_one("#update-options", DataTable)
        key = event.key
        if self.operation is not None:
            if key in {"escape", "p"}:
                self.discard_and_leave()
            else:
                self.app.notify(
                    "Wait for update preparation or press Esc to cancel",
                    severity="warning",
                )
            event.stop()
            return
        if key == "j":
            self.move_table(table, len(self.candidates), 1)
        elif key == "k":
            self.move_table(table, len(self.candidates), -1)
        elif key == "space":
            self.toggle_candidate()
        elif key == "enter":
            self.request_update()
        elif key == "i":
            self.discard_and_navigate(
                lambda: self.app.open_install(self.project_key)
            )
        elif key == "l":
            self.discard_and_navigate(lambda: self.app.open_list(self.project_key))
        elif key == "p":
            self.discard_and_navigate(
                lambda: self.app.open_project(self.project_key)
            )
        elif key == "escape":
            self.discard_and_leave()
        else:
            return
        event.stop()


def parse_args() -> argparse.Namespace:
    return argument_parser().parse_args()


def main() -> int:
    args = parse_args()
    initial_project: str | None = None
    if args.pack:
        initial_project = core.project_key("pack", args.pack)
    elif args.template:
        initial_project = core.project_key("template", args.template)
    if initial_project:
        try:
            core.project_info(initial_project)
        except Exception as error:
            print(error, file=sys.stderr)
            return 2
    HuroshikiApp(initial_project=initial_project).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
