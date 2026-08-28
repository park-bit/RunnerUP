"""Tests for the strict Python code-block parser."""

from app.utils.code_parser import (
    extract_all_python_code,
    extract_python_code,
    has_python_code_block,
)


def test_extracts_python_block():
    msg = "here you go\n```python\nprint('hi')\n```"
    assert extract_python_code(msg) == "print('hi')"


def test_extracts_py_block():
    msg = "```py\nx = 1\nprint(x)\n```"
    assert extract_python_code(msg) == "x = 1\nprint(x)"


def test_language_identifier_is_case_insensitive():
    msg = "```Python\nprint(1)\n```"
    assert extract_python_code(msg) == "print(1)"


def test_returns_first_block_only():
    msg = "```python\nprint('first')\n```\nand\n```python\nprint('second')\n```"
    assert extract_python_code(msg) == "print('first')"


def test_first_supported_block_even_after_other_language():
    msg = "```js\nconsole.log(1)\n```\n```python\nprint('ok')\n```"
    assert extract_python_code(msg) == "print('ok')"


def test_javascript_block_is_ignored():
    msg = "```js\nconsole.log('hi')\n```"
    assert extract_python_code(msg) is None


def test_unmarked_block_ignored_by_default():
    msg = "```\nprint('hi')\n```"
    assert extract_python_code(msg) is None


def test_unmarked_block_allowed_when_configured():
    msg = "```\nprint('hi')\n```"
    assert extract_python_code(msg, allow_unmarked=True) == "print('hi')"


def test_plain_prose_returns_none():
    assert extract_python_code("hello, can someone help with python?") is None


def test_bare_word_python_in_prose_returns_none():
    assert extract_python_code("I love python and writing print statements") is None


def test_command_like_message_returns_none():
    assert extract_python_code("!help") is None
    assert extract_python_code("!run print('hi')") is None


def test_empty_or_none_message_returns_none():
    assert extract_python_code("") is None
    assert extract_python_code(None) is None


def test_empty_code_block_returns_none():
    assert extract_python_code("```python\n\n```") is None
    assert extract_python_code("```python\n   \n```") is None


def test_inline_single_backticks_are_not_a_block():
    assert extract_python_code("`print('hi')`") is None


def test_message_without_any_fence_returns_none():
    assert extract_python_code("just a normal sentence with no code") is None


def test_crlf_line_endings_are_supported():
    msg = "```python\r\nprint('hi')\r\n```"
    assert extract_python_code(msg) == "print('hi')"


def test_indentation_inside_block_is_preserved():
    msg = "```python\nif True:\n    print('x')\n```"
    assert extract_python_code(msg) == "if True:\n    print('x')"


def test_extract_all_returns_every_supported_block():
    msg = "```python\na=1\n```\n```py\nb=2\n```\n```js\nx\n```"
    assert extract_all_python_code(msg) == ["a=1", "b=2"]


def test_extract_all_empty_when_none_supported():
    assert extract_all_python_code("```js\nx\n```") == []


def test_has_python_code_block():
    assert has_python_code_block("```python\nprint(1)\n```") is True
    assert has_python_code_block("nope, nothing here") is False
