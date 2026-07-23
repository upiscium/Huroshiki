#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
from typing import Iterable

try:
    from textual import events, on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container
    from textual.screen import ModalScreen, Screen
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
from packwiz_parser import MenuItem, ParserEvent, visible_menu_items


def enabled_marker(enabled: bool) -> str:
    return "+" if enabled else "-"


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
            core.project_info(self.initial_project)
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

    def open_project(self, project_key: str) -> None:
        core.project_info(project_key)
        self.selected_project = project_key
        self.switch_screen(ProjectScreen(project_key))

    def open_install(self, project_key: str) -> None:
        self.selected_project = project_key
        self.switch_screen(InstallScreen(project_key))

    def open_list(self, project_key: str) -> None:
        self.selected_project = project_key
        self.switch_screen(InstalledModsScreen(project_key))

    def open_update(self, project_key: str) -> None:
        self.selected_project = project_key
        self.switch_screen(UpdateScreen(project_key))

    def open_templates(self, project_key: str) -> None:
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


class MainMenuScreen(BaseScreen):
    screen_title = "huroshiki / Projects"
    help_text = (
        "Tab: focus  Enter: search/open  j/k: move  p: project  "
        "n: new  f: from template  d: delete  r: state  q: quit"
    )

    def __init__(self) -> None:
        super().__init__()
        self.all_projects: list[core.ProjectInfo] = []
        self.visible_projects: list[core.ProjectInfo] = []

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield Input(placeholder="Search projects", id="pack-search")
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
                str(len(core.list_mods(project.key))),
                "yes" if project.enabled else "no",
            )

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
        self.app.open_project(project.key)

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
                self.app.open_state()
            elif event.key == "q":
                self.app.exit()
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
            self.cleanup_confirmed,
        )

    def cleanup_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            report = core.clean_state(apply=True)
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
        self.display_name = self.project.display_name
        self.screen_title = (
            f"{self.project.type_label} / {self.display_name}"
        )
        self.actions = core.project_actions(project_key)
        if self.project.kind == "pack":
            self.help_text = (
                "i: install  l: list  u: update  t: files  "
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
        try:
            with self.app.suspend():
                report = core.create_pack_from_template(**values)
            self.app.push_screen(
                MessageModal(
                    "Template creation result",
                    report.warning_lines,
                ),
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
        elif key == "escape":
            self.app.go_main()
        else:
            return
        event.stop()


class TemplateEditorScreen(BaseScreen):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("escape", "back", "Back", priority=True),
    ]

    def __init__(
        self,
        project_key: str,
        template: core.TemplateInfo,
    ) -> None:
        super().__init__()
        self.project_key = project_key
        self.template = template
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
            self.app.open_templates(self.project_key)
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
            self.app.open_templates(self.project_key)


class TemplateScreen(BaseScreen):
    help_text = (
        "Tab: focus  Enter/e: edit  j/k: move  n: new  "
        "d: delete  r: reload  p: project  Esc: main"
    )

    def __init__(self, project_key: str) -> None:
        super().__init__()
        self.project_key = project_key
        project = core.project_info(project_key)
        self.screen_title = f"{project.display_name} / Files"
        self.all_templates: list[core.TemplateInfo] = []
        self.visible_templates: list[core.TemplateInfo] = []

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield Input(
            placeholder="Filter project files by target or path",
            id="template-search",
        )
        yield DataTable(id="template-table")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#template-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Target", "Path", "Bytes")
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
            )

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
        try:
            self.app.open_template_editor(self.project_key, template)
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
            self.app.open_template_editor(self.project_key, template)
        except Exception as error:
            self.app.notify(str(error), severity="error")

    def request_delete(self) -> None:
        template = self.current_template()
        if template is None:
            self.app.notify("No file is selected", severity="warning")
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
                self.app.go_main()
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
            self.app.open_project(self.project_key)
        elif key == "escape":
            self.app.go_main()
        else:
            return
        event.stop()


class TemplateCandidateScreen(BaseScreen):
    help_text = "j/k: move  Enter: create  Esc: main"

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__()
        self.values = values
        self.screen_title = "Create MODPACK / Select template"
        self.templates: list[core.ProjectInfo] = []

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield Static(
            "Candidates match Minecraft version and loader. "
            "The reference loader version is informational only.",
            id="template-apply-message",
        )
        yield DataTable(id="template-candidate-table")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        table = self.query_one("#template-candidate-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Template", "ID", "Minecraft", "Loader", "Reference", "MODs")
        try:
            self.templates = core.compatible_templates(
                self.values["minecraft"],
                self.values["loader"],
            )
        except Exception as error:
            self.app.notify(str(error), severity="error")
            self.templates = []
        for template in self.templates:
            table.add_row(
                template.display_name,
                template.project_id,
                template.minecraft,
                template.loader,
                template.loader_version,
                str(len(core.list_mods(template.key))),
            )
        if not self.templates:
            self.app.notify(
                "No template matches the selected Minecraft version and loader",
                severity="warning",
            )
        table.focus()

    def create_selected(self) -> None:
        table = self.query_one("#template-candidate-table", DataTable)
        index = self.current_index(table, len(self.templates))
        if index is None:
            return
        template = self.templates[index]
        arguments = dict(self.values)
        arguments["template_id"] = template.project_id
        try:
            with self.app.suspend():
                report = core.create_pack_from_template(**arguments)
            self.app.push_screen(
                MessageModal(
                    "Template creation result",
                    report.warning_lines,
                ),
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
        elif event.key == "enter":
            self.create_selected()
        elif event.key == "escape":
            self.app.go_main()
        else:
            return
        event.stop()


class InstallScreen(BaseScreen):
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
        "q: discard results  j/k: move  c/s: toggle side  b: both  "
        "d: unstage  l: list  u: update  p: project  Esc: main"
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
        self.search_results: list[MenuItem] = []
        self.staged: list[core.ModInfo] = []
        self.operation: core.PackwizAddOperation | None = None
        self.operation_thread: threading.Thread | None = None
        self._closing = False

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield Static("Provider: Modrinth", id="provider-label")
        yield Input(
            placeholder="Search, slug, project ID, or Modrinth URL",
            id="mod-search",
        )
        yield Static("Install side: C +  S +", id="install-side-label")
        yield Static("Enter a search term", id="packwiz-status")
        yield Static("Search results", classes="section-label")
        yield DataTable(id="search-results-table")
        yield Static("Staged changes", classes="section-label")
        yield DataTable(id="staged-table")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        results = self.query_one("#search-results-table", DataTable)
        results.cursor_type = "row"
        results.zebra_stripes = True
        results.add_columns("#", "MOD")

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
        if self.operation is not None and not self.operation.done.is_set():
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
            marker = "*" if item.is_default else ""
            table.add_row(str(item.index), f"{marker}{item.label}")

    def update_side_label(self) -> None:
        self.query_one("#install-side-label", Static).update(
            "Install side: "
            f"C {enabled_marker(self.default_client)}  "
            f"S {enabled_marker(self.default_server)}"
        )

    def set_status(self, message: str) -> None:
        self.query_one("#packwiz-status", Static).update(message)

    def toggle_provider(self) -> None:
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
            "modrinth": "Search, slug, project ID, or Modrinth URL",
            "curseforge": "Search, slug, addon ID, or CurseForge URL",
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

    def discard_search_results(self) -> None:
        if not self.search_results:
            return

        operation = self.operation
        self.search_results = []
        self.refresh_search_results()
        self.set_status("Discarding Packwiz search results...")

        if operation is not None and not operation.done.is_set():
            try:
                operation.cancel_menu()
            except Exception as error:
                self.app.notify(str(error), severity="error")
        else:
            self.query_one("#mod-search", Input).focus()

    @on(Input.Submitted, "#mod-search")
    def start_search(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            self.app.notify("Enter a search term", severity="warning")
            return
        if self.operation is not None and not self.operation.done.is_set():
            self.app.notify("A Packwiz search is already running", severity="warning")
            return

        self.search_results = []
        self.refresh_search_results()
        if self.provider == "url":
            self.set_status("Downloading and staging the self-hosted MOD URL...")
        else:
            self.set_status(f"Starting Packwiz {self.provider} search...")
        event.input.disabled = True

        try:
            operation = self.transaction().begin_add(
                self.provider,
                query,
                client=self.default_client,
                server=self.default_server,
                on_event=self._parser_event_from_worker,
            )
        except Exception as error:
            event.input.disabled = False
            self.set_status(str(error))
            self.app.notify(str(error), severity="error")
            return

        self.operation = operation
        self.operation_thread = threading.Thread(
            target=self._run_operation,
            args=(operation,),
            name=f"huroshiki-packwiz-{self.project_key}",
            daemon=True,
        )
        self.operation_thread.start()

    def _run_operation(self, operation: core.PackwizAddOperation) -> None:
        result = operation.run()
        try:
            self.app.call_from_thread(self._operation_finished, operation, result)
        except Exception:
            # The app or this screen may already be shutting down.
            pass

    def _parser_event_from_worker(self, event: ParserEvent) -> None:
        operation = self.operation
        if event.kind == "confirmation" and operation is not None:
            # The command runs only against a disposable transaction copy.
            # Required dependencies are accepted here and reviewed before apply.
            try:
                operation.confirm(True)
            except (OSError, RuntimeError):
                pass

        try:
            self.app.call_from_thread(self._handle_parser_event, event)
        except Exception:
            pass

    def _handle_parser_event(self, event: ParserEvent) -> None:
        if self._closing:
            return
        if event.kind == "search_started":
            self.set_status(f"Searching {event.message}...")
        elif event.kind == "search_results":
            self.search_results = list(visible_menu_items(event.items))
            self.refresh_search_results()
            if self.search_results:
                self.set_status(
                    f"{len(self.search_results)} result(s); select with j/k and Enter"
                )
                self.query_one("#search-results-table", DataTable).focus()
            else:
                self.set_status("Packwiz returned no selectable results")
        elif event.kind == "confirmation":
            self.set_status("Packwiz dependency confirmation accepted in staging")
        elif event.kind == "diagnostic":
            self.set_status(event.message)
        elif event.kind == "output":
            message = event.message.strip()
            if message and not message[0].isdigit():
                self.set_status(message)

    def _operation_finished(
        self,
        operation: core.PackwizAddOperation,
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

    def current_search_result(self) -> MenuItem | None:
        table = self.query_one("#search-results-table", DataTable)
        index = self.current_index(table, len(self.search_results))
        return None if index is None else self.search_results[index]

    def choose_search_result(self) -> None:
        item = self.current_search_result()
        operation = self.operation
        if item is None or operation is None:
            self.app.notify("No Packwiz result is selected", severity="warning")
            return
        try:
            operation.send_selection(item.index)
            self.set_status(f"Installing {item.label} into the staged transaction...")
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
            self.app.notify("Wait for Packwiz to finish", severity="warning")
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
        focused = self.focused
        results = self.query_one("#search-results-table", DataTable)
        staged = self.query_one("#staged-table", DataTable)

        if isinstance(focused, Input):
            if event.key == "escape":
                if self.operation is not None and not self.operation.done.is_set():
                    self.operation.cancel()
                self.app.go_main()
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
        elif key == "c":
            self.toggle_client(staged=focused is staged)
        elif key == "s":
            self.toggle_server(staged=focused is staged)
        elif key == "b":
            self.enable_both(staged=focused is staged)
        elif focused is staged and key == "d":
            self.remove_staged_mod()
        elif focused is staged and key == "enter":
            self.review()
        elif key == "l":
            self.app.open_list(self.project_key)
        elif key == "u":
            if core.split_project_key(self.project_key)[0] == "pack":
                self.app.open_update(self.project_key)
            else:
                self.app.notify(
                    "Templates resolve compatible versions during MODPACK creation",
                    severity="warning",
                )
        elif key == "p":
            self.app.open_project(self.project_key)
        elif key == "escape":
            if self.operation is not None and not self.operation.done.is_set():
                self.operation.cancel()
            self.app.go_main()
        else:
            return
        event.stop()


class InstalledModsScreen(BaseScreen):
    help_text = (
        "Tab: focus  Enter: filter  j/k: move  Space: select  c/s: toggle side  "
        "b: both  d: delete  i: install  u: update  m: help  p: project  Esc: main"
    )

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
        yield Input(placeholder="Filter installed MODs", id="installed-search")
        yield DataTable(id="installed-table")
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
                enabled_marker(mod.client),
                enabled_marker(mod.server),
                mod.provider,
                str(mod.relative_path),
            )

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
                    [mod.slug for mod in selected],
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
                    "c and s toggle client/server deployment for the highlighted MOD.",
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
                self.app.go_main()
                event.stop()
            return

        key = event.key
        if focused is table and key == "j":
            self.move_table(table, len(self.visible_mods), 1)
        elif focused is table and key == "k":
            self.move_table(table, len(self.visible_mods), -1)
        elif focused is table and key == "space":
            self.toggle_selected()
        elif focused is table and key == "c":
            self.toggle_client()
        elif focused is table and key == "s":
            self.toggle_server()
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
            self.app.open_project(self.project_key)
        elif key == "escape":
            self.app.go_main()
        else:
            return
        event.stop()


class UpdateScreen(BaseScreen):
    help_text = "j/k: move  Space: toggle  Enter: apply  i: install  l: list  Esc: discard"

    def __init__(self, project_key: str) -> None:
        super().__init__()
        self.project_key = project_key
        config = core.project_config(project_key)
        self.display_name = str(config.get("display_name", core.split_project_key(project_key)[1]))
        self.screen_title = f"{self.display_name} / Update"
        self.transaction: core.PackTransaction | None = None
        self.candidates: list[core.UpdateCandidate] = []
        self.selected_paths: set[Path] = set()

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
        table.add_columns("Use", "MOD", "Provider", "Current", "New", "Status")
        try:
            self.transaction = core.PackTransaction.create(self.project_key)
            with self.app.suspend():
                self.candidates = self.transaction.prepare_updates()
            self.selected_paths = {
                candidate.relative_path
                for candidate in self.candidates
                if candidate.available
            }
            self.reload_candidates()
            if not self.selected_paths:
                self.app.notify("No MOD updates are available")
        except Exception as error:
            if self.transaction is not None:
                self.transaction.discard()
                self.transaction = None
            self.app.notify(str(error), severity="error")
        table.focus()

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
                candidate.status,
            )

    def toggle_candidate(self) -> None:
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
        if self.transaction is not None:
            self.transaction.discard()
            self.transaction = None
        self.app.open_project(self.project_key)

    def on_unmount(self) -> None:
        if self.transaction is not None:
            self.transaction.discard()
            self.transaction = None

    def on_key(self, event: events.Key) -> None:
        table = self.query_one("#update-options", DataTable)
        key = event.key
        if key == "j":
            self.move_table(table, len(self.candidates), 1)
        elif key == "k":
            self.move_table(table, len(self.candidates), -1)
        elif key == "space":
            self.toggle_candidate()
        elif key == "enter":
            self.request_update()
        elif key == "i":
            self.app.open_install(self.project_key)
        elif key == "l":
            self.app.open_list(self.project_key)
        elif key == "p":
            self.app.open_project(self.project_key)
        elif key == "escape":
            self.discard_and_leave()
        else:
            return
        event.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Packwiz project TUI")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--pack",
        help="Open this MODPACK project immediately",
    )
    group.add_argument(
        "--template",
        help="Open this template project immediately",
    )
    return parser.parse_args()


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
