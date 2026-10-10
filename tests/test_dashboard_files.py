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
    assert page.status_code == 200 and "text/html" in page.headers["content-type"] and "FleetPilot" in page.text
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
    assert not (wanted - have - {"map-dynamic", "map-static"}), sorted(wanted - have)      # those two are made by the script


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
