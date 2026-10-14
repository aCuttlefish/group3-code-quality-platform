"""Read uploaded Python and documentation as data; never execute source code."""
from __future__ import annotations

import ast
import base64
import binascii
import io
from pathlib import PureWindowsPath
import re
import tokenize

from .runner import validate_source


MAX_UPLOAD_BYTES = 80_000
MAX_SOURCE_CHARS = 20_000
MAX_SOURCE_LINES = 200
MAX_SPEC_CHARS = 2_000
_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)")
_COMMENT_OR_BLANK = re.compile(r"^[ \t\f]*(?:#.*)?$")
_RULE = re.compile(r"(?i)(?<!\w)(R\d+)\s*[:：]")


def _filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("请选择一个有名称的 .py 文件。")
    if any(ord(character) < 32 for character in filename):
        raise ValueError("文件名包含无效控制字符。")
    # PureWindowsPath accepts both Windows and slash-separated browser paths.
    name = PureWindowsPath(filename).name
    if not name or len(name) > 255 or PureWindowsPath(name).suffix.lower() != ".py":
        raise ValueError("仅支持上传单个 .py 文件；请检查文件后缀。")
    return name


def _cookie_matches(code: str) -> list[tuple[int, re.Match]]:
    """Find only PEP 263 cookies in their legally permitted first two lines."""
    lines = code.splitlines(keepends=True)
    if not lines:
        return []
    first = _COOKIE.match(lines[0])
    if first:
        return [(0, first)]
    if len(lines) > 1 and _COMMENT_OR_BLANK.fullmatch(lines[0].rstrip("\r\n")):
        second = _COOKIE.match(lines[1])
        if second:
            return [(1, second)]
    return []


def decode_upload(filename: str, content_base64: str) -> str:
    """Decode a strict base64 .py upload, canonicalizing its cookie to UTF-8.

    The returned Unicode text is suitable for a UTF-8 snapshot and its hash.
    Neither the file path nor any submitted Python code is opened or executed.
    """
    _filename(filename)
    if not isinstance(content_base64, str) or not content_base64:
        raise ValueError("上传内容为空，请重新选择 Python 文件。")
    if len(content_base64) > 4 * ((MAX_UPLOAD_BYTES + 2) // 3):
        raise ValueError("上传文件超过 80000 字节，请缩小文件后重试。")
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError, UnicodeError):
        raise ValueError("上传内容不是有效的 Base64 数据，请重新选择文件。") from None
    if not raw:
        raise ValueError("上传文件为空，请选择包含 Python 函数的文件。")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("上传文件超过 80000 字节，请缩小文件后重试。")
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        code = raw.decode(encoding)
    except (SyntaxError, UnicodeError, LookupError):
        raise ValueError("无法读取 Python 文件编码；请保存为 UTF-8，或在前两行声明正确的编码。") from None
    lines = code.splitlines(keepends=True)
    for index, match in _cookie_matches(code):
        start, end = match.span(1)
        lines[index] = lines[index][:start] + "utf-8" + lines[index][end:]
    return "".join(lines)


def inspect_source(code: str, filename: str | None = None) -> dict:
    """Identify a single supported function and extract comments in source order.

    comment_items contains only line (1-based), kind and text. Kinds are
    module_docstring, function_docstring and comment. Empty items are omitted;
    comments_text joins their text with newlines without adding rule numbers.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("代码为空，请上传或粘贴一个 Python 函数。")
    if len(code.splitlines()) > MAX_SOURCE_LINES or len(code) > MAX_SOURCE_CHARS:
        raise ValueError("仅支持不超过 200 个物理行、20000 个字符的 Python 单文件。")
    safe_name = _filename(filename) if filename is not None else None
    try:
        tree = ast.parse(code, filename=safe_name or "subject.py")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError(f"Python 语法错误：{getattr(exc, 'msg', str(exc))}") from None
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(functions) != 1 or not isinstance(functions[0], ast.FunctionDef):
        raise ValueError("当前仅支持一个顶层同步函数；请保留一个独立的 def 函数。")
    function = functions[0]
    metadata = validate_source(code, function.name)
    ordered_items = []
    for owner, kind in ((tree, "module_docstring"), (function, "function_docstring")):
        text = ast.get_docstring(owner, clean=True)
        if text and text.strip():
            node = owner.body[0]
            ordered_items.append((node.lineno, node.col_offset, {"line": node.lineno, "kind": kind, "text": text.strip()}))
    cookie_lines = {index + 1 for index, _ in _cookie_matches(code)}
    try:
        for token in tokenize.generate_tokens(io.StringIO(code).readline):
            if token.type != tokenize.COMMENT:
                continue
            line, column = token.start
            if line in cookie_lines or (line == 1 and token.string.startswith("#!")):
                continue
            text = token.string[1:].strip()
            if not text or not text.strip("# \t"):
                continue
            ordered_items.append((line, column, {"line": line, "kind": "comment", "text": text}))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        raise ValueError("无法解析 Python 注释，请检查引号、缩进和括号是否完整。") from None
    ordered_items.sort(key=lambda item: (item[0], item[1]))
    items = [item[2] for item in ordered_items]
    return {
        "code": code, "filename": safe_name, "function_name": function.name,
        "metadata": metadata, "comments_text": "\n".join(item["text"] for item in items),
        "comment_items": items,
    }


def normalize_spec(text: str) -> str:
    """Preserve numbered rules or wrap plain prose as one traceable R1 rule."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("说明为空；请填写功能说明，或选择包含有效说明的代码注释。")
    text = text.strip()
    rules = [match.upper() for match in _RULE.findall(text)]
    if len(set(rules)) != len(rules):
        raise ValueError("说明中出现重复的 R 编号；请为每条规则使用唯一编号。")
    if not rules:
        text = "R1：" + text
    if len(text) > MAX_SPEC_CHARS:
        raise ValueError("功能说明加上规则编号后不能超过 2000 个字符，请精简后重试。")
    return text
