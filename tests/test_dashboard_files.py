"""Fast checks on the dashboard's static files (the real-browser checks are in test_ui.py, opt-in)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from manager.server import STATIC_DIR, create_app

HTML = (STATIC_DIR / "index.html").read_text()
JS = (STATIC_DIR / "app.js").read_text()


@pytest.fixture
def client(make_ctrl):
    with TestClient(create_app(make_ctrl())) as c:
        yield c


def test_the_dashboard_is_served_and_the_root_redirects_to_it(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui/"
    page = client.get("/ui/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"] and "MARC" in page.text
    assert "javascript" in client.get("/ui/app.js").headers["content-type"]
    assert "text/css" in client.get("/ui/style.css").headers["content-type"]


def test_static_serving_cannot_read_outside_the_folder(client):
    for path in ("/ui/../server.py", "/ui/%2e%2e/server.py", "/ui/..%2fserver.py"):
        assert client.get(path).status_code == 404


def test_stop_does_not_depend_on_app_js():
    stop = re.search(r'<button id="stop"[^>]*>', HTML, re.S).group(0)
    assert "onclick=" in stop and "/stop" in stop and "POST" in stop


def test_nothing_is_loaded_from_the_internet():
    assert not re.search(r'(src|href)=["\']https?://', HTML) and "https://" not in JS and "http://" not in JS.replace(
        "http://www.w3.org/2000/svg", "")


def test_server_supplied_text_never_goes_through_innerHTML():
    code = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)                  # the comments explain the rule; scan the code
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert banned not in code, banned


def test_every_element_the_script_looks_up_exists_in_the_page():
    wanted = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', JS)) | {i for pair in re.findall(r'\["([a-z]-[xyz])", "([a-z]-[xyz])"\]', JS) for i in pair}
    wanted |= set(re.findall(r'"(light-[a-z]+)"', JS)) | {f"light-{n}" for n in ("rover", "drone", "target")}
    have = set(re.findall(r'id="([A-Za-z0-9_-]+)"', HTML))
    made_by_the_script = {"map-dynamic", "map-static", "plan-skeleton", "crosshair", "xh-v", "xh-h"}
    assert not (wanted - have - made_by_the_script), sorted(wanted - have)


def test_every_manual_button_is_a_valid_manual_request():
    from manager.tools import validate_step
    buttons = re.findall(r"data-m='([^']+)'", HTML)
    assert len(buttons) >= 10
    import json
    for raw in buttons:
        m = json.loads(raw)
        assert validate_step(m["action"], m["robot"], m["args"]) is None, raw


def test_the_manual_form_buttons_send_valid_requests():
    from manager.tools import validate_step
    assert validate_step("drone_goto", "drone", {"x": 1, "y": 1, "z": 0.6}) is None
    assert validate_step("drone_mark", "drone", {"x": 1, "y": 1}) is None
    assert validate_step("rover_goto", "rover", {"x": 1, "y": 1}) is None
    for action, robot in (("drone_goto", "drone"), ("drone_mark", "drone"), ("rover_goto", "rover")):
        assert f'"{action}"' in JS and f'manual("{robot}"' in JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_app_js_is_syntactically_valid():
    r = subprocess.run(["node", "--check", str(Path(STATIC_DIR) / "app.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


CSS = (STATIC_DIR / "style.css").read_text()


def test_fonts_are_bundled_and_every_url_in_the_stylesheet_exists():
    urls = re.findall(r'url\("([^"#]+)"\)', CSS)
    assert len(urls) >= 5 and all(u.startswith("fonts/") for u in urls)         # nothing is fetched from the internet
    for u in urls:
        assert (STATIC_DIR / u).is_file(), u
    assert (STATIC_DIR / "fonts" / "OFL.txt").is_file()                          # the font licence travels with the fonts


def _tokens(block: str) -> set:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))


def test_dark_and_light_themes_define_the_same_colour_tokens():
    dark = re.search(r":root, :root\[data-theme=\"dark\"\] \{(.*?)\n\}", CSS, re.S).group(1)
    light = re.search(r':root\[data-theme="light"\] \{(.*?)\n\}', CSS, re.S).group(1)
    assert _tokens(dark) == _tokens(light) and len(_tokens(dark)) > 25, _tokens(dark) ^ _tokens(light)


def test_the_robot_colours_are_the_ones_that_passed_the_colour_blindness_validator():
    """scout / rover / target were chosen with the dataviz validator (all-pairs, both surfaces: worst CVD delta-E 17
    against a target of 8). Changing them silently would void that; re-run the validator and update both places."""
    dark = re.search(r":root, :root\[data-theme=\"dark\"\] \{(.*?)\n\}", CSS, re.S).group(1)
    light = re.search(r':root\[data-theme="light"\] \{(.*?)\n\}', CSS, re.S).group(1)
    assert "--scout: #3194f2" in dark and "--rover: #d06d15" in dark and "--target: #bd3381" in dark
    assert "--scout: #1f7bcf" in light and "--rover: #b96115" in light and "--target: #932162" in light


def test_motion_respects_reduced_motion_and_keyboard_focus_is_visible():
    assert "prefers-reduced-motion: reduce" in CSS and ":focus-visible" in CSS


def test_the_theme_is_chosen_before_first_paint():
    head = HTML.split("</head>")[0]
    assert 'data-theme="dark"' in HTML and "fp-theme" in head and "prefers-color-scheme" in head


def test_every_icon_the_page_uses_is_in_the_sprite():
    sprite = set(re.findall(r'<symbol id="i-([a-z-]+)"', HTML))
    literal = set(re.findall(r'<use href="#i-([a-z-]+)"', HTML)) | set(re.findall(r'icon\("([a-z-]+)"', JS)) \
        | set(re.findall(r'"#i-([a-z-]+)"', JS)) | set(re.findall(r'ic: "([a-z-]+)"', JS))
    chosen_at_run_time = {"check", "x", "minus", "alert", "play", "info", "target", "stop"}   # step nodes, result chips, badges
    assert not ((literal | chosen_at_run_time) - sprite), (literal | chosen_at_run_time) - sprite
