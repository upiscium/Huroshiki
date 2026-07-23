#!/usr/bin/env python3
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import tomlkit
import yaml

ROOT = Path(__file__).resolve().parents[2]
PACKS = ROOT / "packs"
TEMPLATES = ROOT / "templates"
SHARED = ROOT / "shared"
VALID_SIDES = {"client", "server", "both"}
SIDE_ALIASES = {
    "b": "both",
    "both": "both",
    "c": "client",
    "client": "client",
    "s": "server",
    "server": "server",
}
TARGET_SIDES = {
    "client": {"client", "both"},
    "server": {"server", "both"},
}
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
LOADER_FLAGS = {
    "neoforge": "--neoforge-version",
    "forge": "--forge-version",
    "fabric": "--fabric-version",
    "quilt": "--quilt-version",
}


class ConfigError(RuntimeError):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return value


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def validate_project_id(project_id: str) -> None:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ConfigError(
            "Project ID must use lowercase letters, digits, '.', '_' or '-', "
            "and must start with a letter or digit"
        )


def validate_pack_id(pack_id: str) -> None:
    validate_project_id(pack_id)


def get_pack_root(pack_id: str, *, must_exist: bool = True) -> Path:
    validate_project_id(pack_id)
    root = (PACKS / pack_id).resolve()
    if PACKS.resolve() not in root.parents:
        raise ConfigError("Pack path escaped packs/")
    if must_exist and not root.is_dir():
        raise ConfigError(f"Unknown pack: {pack_id}")
    return root


def load_pack_config(pack_id: str) -> dict[str, Any]:
    root = get_pack_root(pack_id)
    config = merge(load_yaml(root / "pack.yaml"), load_yaml(root / "pack.local.yaml"))
    if config.get("id") != pack_id:
        raise ConfigError(f"packs/{pack_id}/pack.yaml must contain id: {pack_id}")
    return config


def get_template_root(template_id: str, *, must_exist: bool = True) -> Path:
    validate_project_id(template_id)
    root = (TEMPLATES / template_id).resolve()
    if TEMPLATES.resolve() not in root.parents:
        raise ConfigError("Template path escaped templates/")
    if must_exist and not root.is_dir():
        raise ConfigError(f"Unknown template: {template_id}")
    return root


def legacy_template_mods(source: Path) -> list[dict[str, str]]:
    mods: list[dict[str, str]] = []
    for metadata in sorted(source.rglob("*.pw.toml")):
        data = read_toml(metadata)
        update = data.get("update", {})
        if not isinstance(update, dict):
            continue
        provider = ""
        project_id = ""
        for candidate in ("modrinth", "curseforge"):
            value = update.get(candidate)
            if not isinstance(value, dict):
                continue
            for key in (
                "mod-id",
                "project-id",
                "project_id",
                "projectID",
                "projectId",
            ):
                if key in value:
                    provider = candidate
                    project_id = str(value[key])
                    break
            if project_id:
                break
        url = ""
        if not provider or not project_id:
            download = data.get("download", {})
            url = (
                str(download.get("url", "")).strip()
                if isinstance(download, dict)
                else ""
            )
            if url:
                provider = "url"
                project_id = metadata.stem
            else:
                continue
        side = str(data.get("side", "both")).lower()
        if side not in VALID_SIDES:
            side = "both"
        item = {
            "name": str(data.get("name", metadata.stem)),
            "provider": provider,
            "project_id": project_id,
            "side": side,
        }
        if provider == "url":
            item["url"] = url
        mods.append(item)
    return mods


def derive_legacy_template_config(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    source = root / "source"
    pack_path = source / "pack.toml"
    if not pack_path.is_file():
        return config
    data = read_toml(pack_path)
    versions = data.get("versions", {})
    if isinstance(versions, dict):
        config.setdefault("minecraft", str(versions.get("minecraft", "")))
        for loader in LOADER_FLAGS:
            if loader in versions:
                config.setdefault("loader", loader)
                config.setdefault(
                    "reference_loader_version",
                    str(versions[loader]),
                )
                break
    config.setdefault("mods", legacy_template_mods(source))
    return config


def load_template_config(template_id: str) -> dict[str, Any]:
    root = get_template_root(template_id)
    config = load_yaml(root / "template.yaml")
    if config.get("id") != template_id:
        raise ConfigError(
            f"templates/{template_id}/template.yaml must contain id: {template_id}"
        )
    return derive_legacy_template_config(root, config)


def get_project_root(kind: str, project_id: str, *, must_exist: bool = True) -> Path:
    if kind == "pack":
        return get_pack_root(project_id, must_exist=must_exist)
    if kind == "template":
        return get_template_root(project_id, must_exist=must_exist)
    raise ConfigError(f"Unsupported project kind: {kind}")


def load_project_config(kind: str, project_id: str) -> dict[str, Any]:
    if kind == "pack":
        return load_pack_config(project_id)
    if kind == "template":
        return load_template_config(project_id)
    raise ConfigError(f"Unsupported project kind: {kind}")


def require_mapping(mapping: dict[str, Any], key: str, context: str) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{context}.{key} must be a mapping")
    return value


def require_text(mapping: dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def pack_ids() -> list[str]:
    if not PACKS.exists():
        return []
    return sorted(
        path.name
        for path in PACKS.iterdir()
        if path.is_dir() and (path / "pack.yaml").is_file()
    )


def template_ids() -> list[str]:
    if not TEMPLATES.exists():
        return []
    return sorted(
        path.name
        for path in TEMPLATES.iterdir()
        if path.is_dir() and (path / "template.yaml").is_file()
    )


def normalize_template_mod(entry: object, context: str) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise ConfigError(f"{context} must be a mapping")
    provider = str(entry.get("provider", "")).strip().lower()
    if provider not in {"modrinth", "curseforge", "url"}:
        raise ConfigError(f"{context}.provider must be modrinth, curseforge, or url")
    project_id = str(entry.get("project_id", "")).strip()
    if not project_id:
        raise ConfigError(f"{context}.project_id must be a non-empty string")
    name = str(entry.get("name", project_id)).strip() or project_id
    side = str(entry.get("side", "both")).strip().lower()
    if side not in VALID_SIDES:
        raise ConfigError(f"{context}.side must be client, server, or both")
    result = {
        "name": name,
        "provider": provider,
        "project_id": project_id,
        "side": side,
    }
    if provider == "url":
        url = str(entry.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(f"{context}.url must be a public http(s) URL")
        result["url"] = url
    return result


def template_mods(template_id: str) -> list[dict[str, str]]:
    config = load_template_config(template_id)
    value = config.get("mods", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"templates/{template_id}/template.yaml mods must be a list")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(value):
        normalized = normalize_template_mod(entry, f"mods[{index}]")
        key = (normalized["provider"], normalized["project_id"])
        if key in seen:
            continue
        result.append(normalized)
        seen.add(key)
    return result


def save_template_mods(template_id: str, mods: list[dict[str, str]]) -> None:
    root = get_template_root(template_id)
    config_path = root / "template.yaml"
    config = load_yaml(config_path)
    normalized = [
        normalize_template_mod(entry, f"mods[{index}]")
        for index, entry in enumerate(mods)
    ]
    deduplicated: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in normalized:
        key = (entry["provider"], entry["project_id"])
        if key in seen:
            continue
        deduplicated.append(entry)
        seen.add(key)
    config["mods"] = deduplicated
    temporary = config_path.with_name(".template.yaml.huroshiki-tmp")
    temporary.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.replace(config_path)


def template_versions(template_id: str) -> tuple[str, str, str]:
    config = load_template_config(template_id)
    minecraft = require_text(config, "minecraft", template_id)
    loader = require_text(config, "loader", template_id).lower()
    if loader not in LOADER_FLAGS:
        raise ConfigError(f"Unsupported loader in template {template_id}: {loader}")
    loader_version = str(config.get("reference_loader_version", "")).strip()
    if not loader_version:
        raise ConfigError(
            f"templates/{template_id}/template.yaml must contain reference_loader_version"
        )
    return minecraft, loader, loader_version


def compatible_template_ids(minecraft: str, loader: str) -> list[str]:
    normalized_loader = loader.strip().lower()
    result: list[str] = []
    for template_id in template_ids():
        try:
            template_minecraft, template_loader, _ = template_versions(template_id)
        except ConfigError:
            continue
        if template_minecraft == minecraft and template_loader == normalized_loader:
            result.append(template_id)
    return result


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(shlex.quote(part) for part in command))
    subprocess.run(command, cwd=cwd, check=True)


def copy_tree(source: Path, destination: Path) -> None:
    if source.exists():
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".gitkeep"),
        )


def metadata_files(source: Path) -> list[Path]:
    return sorted(source.rglob("*.pw.toml"))


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def normalize_side(side: str) -> str:
    normalized = SIDE_ALIASES.get(side.lower())
    if normalized is None:
        accepted = "both|b, client|c, server|s"
        raise ConfigError(f"Invalid side {side!r}; expected {accepted}")
    return normalized


def set_side_file(path: Path, side: str) -> None:
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    doc["side"] = normalize_side(side)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def set_side_and_refresh(source: Path, path: Path, side: str) -> None:
    snapshots = {
        item: item.read_bytes() if item.exists() else None
        for item in (path, source / "index.toml", source / "pack.toml")
    }
    try:
        set_side_file(path, side)
        result = subprocess.run(
            ["packwiz", "refresh"],
            cwd=source,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ConfigError(result.stderr.strip() or "packwiz refresh failed")
    except Exception:
        for item, content in snapshots.items():
            if content is None:
                item.unlink(missing_ok=True)
            else:
                temporary = item.with_name(f".{item.name}.huroshiki-side-rollback")
                temporary.write_bytes(content)
                temporary.replace(item)
        raise


def metadata_snapshot(source: Path) -> dict[Path, bytes]:
    return {path.resolve(): path.read_bytes() for path in metadata_files(source)}


def changed_metadata_files(
    before: dict[Path, bytes],
    source: Path,
) -> list[Path]:
    changed: list[Path] = []

    for path in metadata_files(source):
        resolved = path.resolve()
        previous = before.get(resolved)
        current = path.read_bytes()

        if previous is None or previous != current:
            changed.append(path)

    return sorted(changed)


def direct_project_selector(query: str) -> tuple[str, str] | None:
    lowered = query.lower()

    if lowered.startswith("mr:"):
        value = query[3:].strip()
        if not value:
            raise ConfigError("mr: requires a Modrinth project ID, slug or URL")
        return "modrinth", value

    if lowered.startswith("cf:"):
        value = query[3:].strip()
        if not value:
            raise ConfigError("cf: requires a CurseForge project ID, slug or URL")
        return "curseforge", value

    if "modrinth.com/" in lowered:
        return "modrinth", query

    if "curseforge.com/" in lowered:
        return "curseforge", query

    return None


def choose_provider() -> str | None:
    if not sys.stdin.isatty():
        raise ConfigError(
            "Provider selection requires a terminal. "
            "Use mr:<project>, cf:<project>, or a project URL."
        )

    print("Select provider:")
    print("  1. Modrinth")
    print("  2. CurseForge")
    print("  q. Cancel")

    while True:
        answer = input("Provider [1/2/q]: ").strip().lower()

        if answer in {"1", "m", "mr", "modrinth"}:
            return "modrinth"
        if answer in {"2", "c", "cf", "curseforge"}:
            return "curseforge"
        if answer in {"q", "quit", "cancel"}:
            return None

        print("Enter 1, 2, or q.")


def install_and_classify(
    pack_id: str,
    provider: str,
    selector: str,
    side: str,
) -> int:
    normalized_side = normalize_side(side)
    source = get_pack_root(pack_id) / "source"
    before = metadata_snapshot(source)

    if provider == "modrinth":
        command = ["packwiz", "modrinth", "add", selector]
    elif provider == "curseforge":
        if selector.isdecimal():
            command = [
                "packwiz",
                "curseforge",
                "add",
                "--addon-id",
                selector,
            ]
        else:
            command = ["packwiz", "curseforge", "add", selector]
    else:
        raise ConfigError(f"Unsupported provider: {provider}")

    run(command, cwd=source)

    changed = changed_metadata_files(before, source)
    if not changed:
        raise ConfigError(
            "Packwiz did not create or modify any .pw.toml files. "
            "The project may already be installed; use `just side` "
            "to change its side."
        )

    print(f"Assigning side = {normalized_side}:")
    for metadata in changed:
        set_side_file(metadata, normalized_side)
        print(f"  {metadata.relative_to(source)}")

    run(["packwiz", "refresh"], cwd=source)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    direct = direct_project_selector(args.query)

    if direct is not None:
        provider, selector = direct
    else:
        provider = choose_provider()
        if provider is None:
            print("Cancelled.")
            return 0
        selector = args.query

    print(f"Using Packwiz {provider} search/install.")
    return install_and_classify(
        args.pack,
        provider,
        selector,
        args.side,
    )


ACTIVE_PACK_ENV = "MODPACK"


def active_pack_id() -> str:
    pack_id = os.environ.get(ACTIVE_PACK_ENV, "").strip()
    if not pack_id:
        raise ConfigError(
            "No MODPACK is selected. Run `just use <MODPACK>` first, "
            "or use an explicit `*-for` recipe."
        )

    get_pack_root(pack_id)
    return pack_id


def cmd_use(args: argparse.Namespace) -> int:
    pack_id = args.pack
    pack_root = get_pack_root(pack_id)
    config = load_pack_config(pack_id)

    shell = os.environ.get("SHELL", "").strip()
    if not shell:
        shell = shutil.which("zsh") or shutil.which("bash") or "/bin/sh"

    environment = os.environ.copy()
    environment[ACTIVE_PACK_ENV] = pack_id
    environment["MODPACK_ROOT"] = str(pack_root)

    display_name = config.get("display_name", pack_id)
    print(f"Entering MODPACK context: {pack_id} ({display_name})")
    print("Run `exit` to leave this context.")
    print("Use `just current` to confirm the selected pack.")

    result = subprocess.run(
        [shell, "-i"],
        cwd=ROOT,
        env=environment,
        check=False,
    )
    return result.returncode


def cmd_current(_: argparse.Namespace) -> int:
    pack_id = active_pack_id()
    config = load_pack_config(pack_id)
    print(f"{pack_id}\t{config.get('display_name', '')}")
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    ids = pack_ids()
    if not ids:
        print("No packs are configured")
        return 0
    print(f"{'PACK':30} {'ENABLED':8} DISPLAY NAME")
    print(f"{'-' * 30} {'-' * 8} {'-' * 30}")
    for pack_id in ids:
        config = load_pack_config(pack_id)
        enabled = "yes" if config.get("enabled", True) else "no"
        print(f"{pack_id:30} {enabled:8} {config.get('display_name', '')}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    import pprint

    root = get_pack_root(args.pack)
    print("== configuration ==")
    pprint.pp(load_pack_config(args.pack), sort_dicts=False)
    print("\n== Packwiz metadata ==")
    pprint.pp(read_toml(root / "source" / "pack.toml"), sort_dicts=False)
    return 0


def create_layout(root: Path) -> None:
    directories = [
        "source/mods",
        "content/common/config",
        "content/common/defaultconfigs",
        "content/common/kubejs/startup_scripts",
        "content/client/config",
        "content/client/kubejs/client_scripts",
        "content/client/resourcepacks",
        "content/client/shaderpacks",
        "content/server/config",
        "content/server/defaultconfigs",
        "content/server/kubejs/server_scripts",
    ]
    for relative in directories:
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").touch()


def init_packwiz_project(
    root: Path,
    *,
    display_name: str,
    minecraft: str,
    loader: str,
    loader_version: str,
) -> None:
    if loader not in LOADER_FLAGS:
        raise ConfigError(f"Unsupported loader: {loader}")
    source = root / "source"
    source.mkdir(parents=True)
    command = [
        "packwiz",
        "--yes",
        "init",
        "--name",
        display_name,
        "--author",
        "upiscium",
        "--version",
        "0.1.0",
        "--mc-version",
        minecraft,
        "--modloader",
        loader,
        LOADER_FLAGS[loader],
        loader_version,
    ]
    run(command, cwd=source)
    create_layout(root)
    (source / ".packwizignore").write_text(
        "*.log\n*.gitkeep\n/crash-reports/\n/logs/\n/saves/\n/screenshots/\n/world/\n",
        encoding="utf-8",
    )


def cmd_new(args: argparse.Namespace) -> int:
    root = get_pack_root(args.pack, must_exist=False)
    if root.exists():
        raise ConfigError(f"Pack already exists: {args.pack}")
    try:
        init_packwiz_project(
            root,
            display_name=args.display_name,
            minecraft=args.minecraft,
            loader=args.loader,
            loader_version=args.loader_version,
        )
        pack_yaml = (
            f"id: {args.pack}\n"
            f"display_name: {args.display_name}\n"
            "enabled: true\n"
            "distribution:\n"
            f"  rsync_target: dockge:/opt/stacks/packwiz-web/packs/{args.pack}\n\n"
            "minecraft_server:\n"
            "  ssh_host: minecraft\n"
            f"  stack_dir: /opt/stacks/{args.pack}\n"
            f"  service: {args.pack}\n"
        )
        (root / "pack.yaml").write_text(pack_yaml, encoding="utf-8")
        (root / "pack.local.yaml.example").write_text(
            "# Copy to pack.local.yaml for machine-local overrides.\n",
            encoding="utf-8",
        )
        (root / "profiles.yaml").write_text("profiles: {}\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    print(f"Created packs/{args.pack}")
    return 0


def cmd_new_template(args: argparse.Namespace) -> int:
    root = get_template_root(args.template, must_exist=False)
    if root.exists():
        raise ConfigError(f"Template already exists: {args.template}")
    try:
        root.mkdir(parents=True)
        template_yaml = {
            "id": args.template,
            "display_name": args.display_name,
            "enabled": True,
            "minecraft": args.minecraft,
            "loader": args.loader,
            "reference_loader_version": args.loader_version,
            "mods": [],
        }
        (root / "template.yaml").write_text(
            yaml.safe_dump(template_yaml, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    print(f"Created templates/{args.template}")
    return 0


def cmd_migrate_template(args: argparse.Namespace) -> int:
    root = get_template_root(args.template)
    config = load_template_config(args.template)
    config_path = root / "template.yaml"
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(
        f"Migrated templates/{args.template}/template.yaml to MOD-list format; "
        "legacy source/ files were left untouched"
    )
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    source = get_pack_root(args.pack) / "source"
    run(["packwiz", "remove", args.mod], cwd=source)
    return 0


def cmd_side(args: argparse.Namespace) -> int:
    side = normalize_side(args.side)
    source = (get_pack_root(args.pack) / "source").resolve()
    target = (source / args.metadata_file).resolve()
    if source not in target.parents:
        raise ConfigError("Metadata path escaped source/")
    if not target.is_file() or not target.name.endswith(".pw.toml"):
        raise ConfigError(f"Metadata file not found: {target}")
    set_side_and_refresh(source, target, side)
    print(f"{args.pack}/{target.relative_to(source)}: side = {side}")
    return 0


def resolve_modrinth(project: str) -> str:
    request = Request(
        f"https://api.modrinth.com/v2/project/{quote(project, safe='')}",
        headers={"User-Agent": "upiscium-packwiz-monorepo/1.0"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.load(response)
    except (HTTPError, URLError, TimeoutError) as error:
        raise ConfigError(
            f"Could not resolve Modrinth project {project!r}: {error}"
        ) from error
    project_id = data.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise ConfigError(f"No Modrinth project ID returned for {project!r}")
    return project_id


def find_metadata(source: Path, provider: str, project_id: str | int) -> Path | None:
    for path in metadata_files(source):
        update = read_toml(path).get("update", {})
        if provider == "modrinth":
            value = update.get("modrinth", {}).get("mod-id")
            if str(value or "") == str(project_id):
                return path
        else:
            value = update.get("curseforge", {}).get("project-id")
            if value == project_id:
                return path
    return None


def apply_profile_entry(source: Path, entry: dict[str, Any]) -> None:
    provider = entry.get("source")
    project = entry.get("project")
    side = entry.get("side")
    if provider not in {"modrinth", "curseforge"}:
        raise ConfigError(f"Unsupported profile source: {provider!r}")
    if project is None:
        raise ConfigError("Profile entry is missing project")
    if side not in VALID_SIDES:
        raise ConfigError(f"Invalid/missing side for {project!r}: {side!r}")

    if provider == "modrinth":
        project_id: str | int = resolve_modrinth(str(project))
        metadata = find_metadata(source, provider, project_id)
        if metadata is None:
            run(
                [
                    "packwiz",
                    "--yes",
                    "modrinth",
                    "add",
                    "--project-id",
                    str(project_id),
                ],
                cwd=source,
            )
            metadata = find_metadata(source, provider, project_id)
    else:
        try:
            project_id = int(project)
        except (TypeError, ValueError) as error:
            raise ConfigError(
                "CurseForge profiles require numeric project IDs"
            ) from error
        metadata = find_metadata(source, provider, project_id)
        if metadata is None:
            run(
                [
                    "packwiz",
                    "--yes",
                    "curseforge",
                    "add",
                    "--addon-id",
                    str(project_id),
                ],
                cwd=source,
            )
            metadata = find_metadata(source, provider, project_id)

    if metadata is None:
        raise ConfigError(f"Metadata not found after adding {provider}:{project}")
    set_side_file(metadata, side)
    print(f"  {metadata.relative_to(source)} -> {side}")


def cmd_profile(args: argparse.Namespace) -> int:
    root = get_pack_root(args.pack)
    profiles = merge(
        load_yaml(SHARED / "profiles.yaml"),
        load_yaml(root / "profiles.yaml"),
    ).get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("Merged profiles must be a mapping")
    source = root / "source"
    for name in args.names:
        if name not in profiles:
            raise ConfigError(
                f"Unknown profile {name!r}; available: {', '.join(sorted(profiles))}"
            )
        entries = profiles[name] or []
        if not isinstance(entries, list):
            raise ConfigError(f"Profile {name!r} must be a list")
        print(f"== Applying {name} to {args.pack} ==")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigError(f"Invalid profile entry: {entry!r}")
            apply_profile_entry(source, entry)
    run(["packwiz", "refresh"], cwd=source)
    return 0


def project_versions(source: Path) -> tuple[str, str, str]:
    data = read_toml(source / "pack.toml")
    versions = data.get("versions", {})
    if not isinstance(versions, dict):
        raise ConfigError(f"{source}/pack.toml versions must be a mapping")
    minecraft = str(versions.get("minecraft", ""))
    loaders = [
        (loader, str(versions[loader])) for loader in LOADER_FLAGS if loader in versions
    ]
    if len(loaders) != 1:
        raise ConfigError(
            f"{source}/pack.toml must define exactly one supported loader"
        )
    loader, loader_version = loaders[0]
    return minecraft, loader, loader_version


def copy_metadata(source: Path, destination: Path) -> None:
    for metadata in metadata_files(source):
        relative = metadata.relative_to(source)
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(metadata, output)


def build_target(root: Path, target: str) -> list[str]:
    source = root / "source"
    destination = root / "dist" / target
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    shutil.copy2(source / "pack.toml", destination / "pack.toml")
    (destination / "index.toml").write_text(
        'hash-format = "sha256"\n',
        encoding="utf-8",
    )
    copy_metadata(source, destination)

    errors: list[str] = []
    for metadata in metadata_files(destination):
        side = read_toml(metadata).get("side")
        if side not in VALID_SIDES:
            errors.append(f"{metadata.relative_to(ROOT)} has no valid side")
            continue
        if side not in TARGET_SIDES[target]:
            metadata.unlink()

    copy_tree(root / "content" / "common", destination)
    copy_tree(root / "content" / target, destination)

    if not errors:
        run(["packwiz", "refresh"], cwd=destination)
    return errors


def build_pack(pack_id: str) -> int:
    root = get_pack_root(pack_id)
    for required in (root / "source" / "pack.toml", root / "source" / "index.toml"):
        if not required.is_file():
            raise ConfigError(f"Missing required file: {required}")
    errors = build_target(root, "client") + build_target(root, "server")
    if errors:
        print(
            "Build stopped because side classification is incomplete:", file=sys.stderr
        )
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            f"Use: just side {pack_id} mods/<name>.pw.toml client|server|both",
            file=sys.stderr,
        )
        return 1
    print(f"Built {pack_id}: packs/{pack_id}/dist/client and server")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    return build_pack(args.pack)


def cmd_build_all(_: argparse.Namespace) -> int:
    failed: list[str] = []
    for pack_id in pack_ids():
        if not load_pack_config(pack_id).get("enabled", True):
            print(f"Skipping disabled pack: {pack_id}")
            continue
        print(f"== Building {pack_id} ==")
        if build_pack(pack_id) != 0:
            failed.append(pack_id)
    if failed:
        print("Failed packs:", ", ".join(failed), file=sys.stderr)
        return 1
    return 0


def validate_template(template_id: str) -> int:
    minecraft, loader, loader_version = template_versions(template_id)
    mods = template_mods(template_id)
    print(
        f"Validated template {template_id}: {minecraft}, {loader} "
        f"reference {loader_version}, {len(mods)} MOD(s)"
    )
    return 0


def cmd_validate_template(args: argparse.Namespace) -> int:
    return validate_template(args.template)


def distribution_target(pack_id: str) -> str:
    config = load_pack_config(pack_id)
    distribution = require_mapping(config, "distribution", pack_id)
    return require_text(
        distribution,
        "rsync_target",
        f"{pack_id}.distribution",
    )


def deploy_pack(pack_id: str, *, build: bool = False) -> int:
    if build and build_pack(pack_id) != 0:
        return 1
    root = get_pack_root(pack_id)
    target = distribution_target(pack_id)
    dist = root / "dist"
    for side in ("client", "server"):
        if not (dist / side / "pack.toml").is_file():
            raise ConfigError(f"{side} distribution is not built for {pack_id}")
    run(["rsync", "-av", "--delete", f"{dist}/", target.rstrip("/") + "/"])
    print(f"Deployed {pack_id} to {target}")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    return deploy_pack(args.pack)


def cmd_restart(args: argparse.Namespace) -> int:
    config = load_pack_config(args.pack)
    server = require_mapping(config, "minecraft_server", args.pack)
    host = require_text(server, "ssh_host", f"{args.pack}.minecraft_server")
    stack = require_text(server, "stack_dir", f"{args.pack}.minecraft_server")
    service = require_text(server, "service", f"{args.pack}.minecraft_server")
    remote = f"cd {shlex.quote(stack)} && docker compose restart {shlex.quote(service)}"
    run(["ssh", host, remote])
    return 0


def cmd_deploy_all(_: argparse.Namespace) -> int:
    failed: list[str] = []
    for pack_id in pack_ids():
        if not load_pack_config(pack_id).get("enabled", True):
            print(f"Skipping disabled pack: {pack_id}")
            continue
        print(f"== Building/deploying {pack_id} ==")
        try:
            if deploy_pack(pack_id, build=True) != 0:
                failed.append(pack_id)
        except Exception as error:
            print(f"{pack_id}: {error}", file=sys.stderr)
            failed.append(pack_id)
    if failed:
        print("Failed deployments:", ", ".join(failed), file=sys.stderr)
        return 1
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    if args.kind == "packs":
        for pack_id in pack_ids():
            print(pack_id)
        return 0

    if args.kind == "templates":
        for template_id in template_ids():
            print(template_id)
        return 0

    if args.pack is None:
        raise ConfigError(f"Completion kind {args.kind!r} requires a pack ID")

    root = get_pack_root(args.pack)

    if args.kind == "metadata":
        source = root / "source"
        for path in metadata_files(source):
            print(path.relative_to(source))
        return 0

    if args.kind == "mods":
        source = root / "source"
        for path in metadata_files(source):
            print(path.name.removesuffix(".pw.toml"))
        return 0

    if args.kind == "profiles":
        profiles = merge(
            load_yaml(SHARED / "profiles.yaml"),
            load_yaml(root / "profiles.yaml"),
        ).get("profiles", {})

        if not isinstance(profiles, dict):
            raise ConfigError("Merged profiles must be a mapping")

        for name in sorted(profiles):
            print(name)
        return 0

    raise ConfigError(f"Unsupported completion kind: {args.kind}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Manage multiple Packwiz projects")
    sub = root.add_subparsers(dest="command", required=True)

    item = sub.add_parser("complete")
    item.add_argument(
        "kind", choices=["packs", "templates", "profiles", "metadata", "mods"]
    )
    item.add_argument("pack", nargs="?")
    item.set_defaults(func=cmd_complete)
    item = sub.add_parser("use")
    item.add_argument("pack")
    item.set_defaults(func=cmd_use)
    item = sub.add_parser("current")
    item.set_defaults(func=cmd_current)
    item = sub.add_parser("list")
    item.set_defaults(func=cmd_list)
    item = sub.add_parser("show")
    item.add_argument("pack")
    item.set_defaults(func=cmd_show)
    item = sub.add_parser("new")
    item.add_argument("pack")
    item.add_argument("display_name")
    item.add_argument("minecraft")
    item.add_argument("loader", choices=sorted(LOADER_FLAGS))
    item.add_argument("loader_version")
    item.set_defaults(func=cmd_new)
    item = sub.add_parser("new-template")
    item.add_argument("template")
    item.add_argument("display_name")
    item.add_argument("minecraft")
    item.add_argument("loader", choices=sorted(LOADER_FLAGS))
    item.add_argument("loader_version")
    item.set_defaults(func=cmd_new_template)
    item = sub.add_parser("validate-template")
    item.add_argument("template")
    item.set_defaults(func=cmd_validate_template)
    item = sub.add_parser("migrate-template")
    item.add_argument("template")
    item.set_defaults(func=cmd_migrate_template)
    item = sub.add_parser("add")
    item.add_argument("pack")
    item.add_argument("query")
    item.add_argument("side")
    item.set_defaults(func=cmd_add)
    item = sub.add_parser("remove")
    item.add_argument("pack")
    item.add_argument("mod")
    item.set_defaults(func=cmd_remove)
    item = sub.add_parser("side")
    item.add_argument("pack")
    item.add_argument("metadata_file")
    item.add_argument("side")
    item.set_defaults(func=cmd_side)
    item = sub.add_parser("profile")
    item.add_argument("pack")
    item.add_argument("names", nargs="+")
    item.set_defaults(func=cmd_profile)
    item = sub.add_parser("build")
    item.add_argument("pack")
    item.set_defaults(func=cmd_build)
    item = sub.add_parser("build-all")
    item.set_defaults(func=cmd_build_all)
    item = sub.add_parser("deploy")
    item.add_argument("pack")
    item.set_defaults(func=cmd_deploy)
    item = sub.add_parser("restart")
    item.add_argument("pack")
    item.set_defaults(func=cmd_restart)
    item = sub.add_parser("deploy-all")
    item.set_defaults(func=cmd_deploy_all)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"command failed with exit code {error.returncode}", file=sys.stderr)
        return error.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
