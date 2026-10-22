"""om_extract._js.js_json(): JSON as JavaScript's JSON.stringify writes it (numbers, layout, undefined keys)."""
from om_extract._js import UNDEFINED, js_json


def test_numbers_print_as_javascript_does():
  assert js_json([-180.0, 5.0, 0.5, 1e-7, 1e21, 0.000001, 3]) == "[-180,5,0.5,1e-7,1e+21,0.000001,3]"


def test_non_finite_numbers_are_null_and_none_is_null():
  assert js_json([float("nan"), float("inf"), None]) == "[null,null,null]"


def test_strings_keep_non_ascii_and_escape_quotes():
  assert js_json(["é", 's."CRW_SST"']) == '["é","s.\\"CRW_SST\\""]'


def test_indent_puts_every_item_on_its_own_line_and_empty_stays_inline():
  got = js_json({"a": [1, [2, 3]], "b": {}, "c": [], "d": "x"}, indent=2)
  assert got == '{\n  "a": [\n    1,\n    [\n      2,\n      3\n    ]\n  ],\n  "b": {},\n  "c": [],\n  "d": "x"\n}'


def test_an_undefined_value_drops_its_key_as_json_stringify_does():
  assert js_json({"a": 1, "b": UNDEFINED, "c": False}) == '{"a":1,"c":false}'
