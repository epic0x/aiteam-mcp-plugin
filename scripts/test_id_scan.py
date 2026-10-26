"""Self-test for scripts/id_scan.py's product-name exception. Run: python3 scripts/test_id_scan.py (CI: id-scan).

Fixture names are built from pieces so this file never trips the real deny list."""
import importlib.util
import pathlib
import re

spec = importlib.util.spec_from_file_location("id_scan", pathlib.Path(__file__).with_name("id_scan.py"))
ids = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ids)

FIRST = "Dav" + "is"                                    # the product family name
OTHER = "Zor" + "blat"                                  # stands in for any other team name
DENY = [re.compile(r"\b" + FIRST + r"\b", re.I), re.compile(r"\b" + OTHER + r"\b", re.I)]
INTRO = "connector/aiteam_connector/intro.py"


def hits(line, rel):
    return ids.scan_line(line, DENY, rel)


def test_exact_product_phrases_pass_in_product_files():
    for line in (f"We are {FIRST}, and we are all Sons of {FIRST}.", f"Who we are: Sons of {FIRST}",
                 f"(the Sons of {FIRST}, introduced on connect)", f"customer gets (Son of {FIRST}, real sessions"):
        assert hits(line, INTRO) == [], line


def test_bare_name_still_fails_in_product_files():
    assert hits(f"ask {FIRST} to deploy", INTRO)
    assert hits(f"sons of {FIRST.lower()}", INTRO)                  # wrong case is not the product phrase
    assert hits(f"Sons of {FIRST}. Also {FIRST} runs the box.", INTRO)   # phrase + bare name on one line


def test_other_team_names_still_fail_in_product_files():
    assert hits(f"Sons of {FIRST} and {OTHER}", INTRO)
    assert hits(f"{OTHER} merged it", "README.md")


def test_product_phrase_fails_outside_product_files():
    for rel in ("connector/aiteam_connector/server.py", "docs/AUTH-DESIGN.md", "other.md", ""):
        assert hits(f"Sons of {FIRST}", rel), rel


def test_product_files_exist():
    root = pathlib.Path(__file__).resolve().parents[1]
    missing = [f for f in ids.PRODUCT_FILES if not (root / f).is_file()]
    assert not missing, f"stale PRODUCT_FILES entries: {missing}"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"id_scan self-test: {n} passed")
