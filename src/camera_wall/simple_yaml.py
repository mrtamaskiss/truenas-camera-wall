from __future__ import annotations

import re
from typing import Any

from .config import ConfigError


_INT_RE = re.compile(r"^-?\d+$")


def parse_simple_yaml(text: str) -> Any:
    """Parse the small YAML subset used by config.example.yaml.

    Runtime images install PyYAML. This fallback keeps local unit tests runnable
    on minimal systems where PyYAML is not present.
    """

    lines = _preprocess(text)
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise ConfigError("Could not parse YAML input")
    return value


def _preprocess(text: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip(" \t"))]:
            raise ConfigError("Tabs are not supported in indentation")
        result.append((len(raw_line) - len(raw_line.lstrip(" ")), raw_line.strip()))
    return result


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if _is_list_item(lines[index][1]):
        return _parse_list(lines, index, indent)
    return _parse_dict(lines, index, indent)


def _parse_dict(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        current_indent, text = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ConfigError(f"Unexpected indentation near: {text}")
        if _is_list_item(text):
            break
        key, value = _split_key_value(text)
        if value == "":
            if index + 1 >= len(lines) or lines[index + 1][0] <= current_indent:
                result[key] = {}
                index += 1
            else:
                result[key], index = _parse_block(lines, index + 1, lines[index + 1][0])
        else:
            result[key] = _parse_scalar(value)
            index += 1
    return result, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        current_indent, text = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ConfigError(f"Unexpected indentation near: {text}")
        if not _is_list_item(text):
            break

        item_text = text[1:].strip()
        if item_text == "":
            value, index = _parse_block(lines, index + 1, current_indent + 2)
            result.append(value)
            continue

        if ":" in item_text and not item_text.startswith(("'", '"')):
            key, value = _split_key_value(item_text)
            item: dict[str, Any] = {key: _parse_scalar(value)} if value != "" else {key: {}}
            index += 1
            if index < len(lines) and lines[index][0] > current_indent:
                nested, index = _parse_dict(lines, index, lines[index][0])
                item.update(nested)
            result.append(item)
        else:
            result.append(_parse_scalar(item_text))
            index += 1
    return result, index


def _split_key_value(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ConfigError(f"Expected key/value pair near: {text}")
    key, value = text.split(":", 1)
    key = key.strip()
    if not key:
        raise ConfigError(f"Missing key near: {text}")
    return key, value.strip()


def _parse_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "Null", "~"}:
        return None
    if _INT_RE.match(value):
        return int(value)
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace(r"\"", '"')
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _is_list_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")
