"""The dashboard in a real browser against the real server on mock robots. Opt-in:

    uv pip install --python .venv/bin/python -r requirements-ui-tests.txt && .venv/bin/playwright install chromium
    .venv/bin/python -m pytest -m ui

These checks exist because the unit tests cannot see layout: a first version buried the authorization question
below a 16-row plan, where the DOM test passed and a human would never have noticed it.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = pytest.mark.ui
ROOT = Path(__file__).resolve().parent.parent
VIEWPORT = {"width": 1280, "height": 900}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1", "MOCK_TIME_SCALE": "12",
           "RUNS_DIR": str(tmp_path_factory.mktemp("runs"))}
    env.pop("GEMINI_API_KEY", None)
    proc = subprocess.Popen([sys.executable, "-m", "manager.server"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"

    def state():
        with urllib.request.urlopen(base + "/state", timeout=3) as r:
            return json.loads(r.read())

    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            state()
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("the server did not start")

    def wait(pred, timeout=60, what="state"):
        t = time.time()
        while time.time() - t < timeout:
            s = state()
            if pred(s):
                return s
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what}: {state()['mission']}")

    yield type("Server", (), {"base": base, "state": staticmethod(state), "wait": staticmethod(wait)})
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def page(server):
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = browser.new_page(viewport=VIEWPORT)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "status of 409" not in m.text else None)
        pg.goto(server.base + "/")
        pg.wait_for_selector("#map-dynamic", state="attached", timeout=8000)
        pg.wait_for_function("document.querySelectorAll('#scenarios li').length >= 5", timeout=8000)
        pg.wait_for_function("document.querySelector('#light-rover').classList.contains('on')", timeout=8000)
        pg.errors = errors
        yield pg
        browser.close()


def test_the_page_draws_the_field_and_the_robots(page, server):
    assert page.url.endswith("/ui/")                                          # GET / redirects
    assert page.locator("#light-drone.on").count() == 1 and page.locator("#light-target.on").count() == 1
    assert page.locator("#map-dynamic polygon.rover").count() == 1            # rover triangle
    assert page.locator("#map-dynamic circle.truth").count() == 1             # the target's ground truth
    assert page.locator("#map .zone.disposal").count() == 1
    assert page.locator("#approve").is_disabled()


def test_mentor_toggle_and_manual_control(page, server):
    page.check("#mentor")
    server.wait(lambda s: s["mentor_present"], 8, "mentor")
    page.click("text=Fwd 0.3 m")
    s = server.wait(lambda s: (s["manual"] or {}).get("status") in ("done", "failed"), 15, "manual drive")
    assert s["manual"]["action"] == "rover_drive" and s["manual"]["status"] == "done"
    page.click("text=Snapshot")
    server.wait(lambda s: s["last_image_id"], 10, "snapshot")
    page.wait_for_function("document.querySelector('#camera').src.includes('/images/img_')", timeout=5000)
    assert page.locator("#camera").evaluate("i => i.complete && i.naturalWidth > 0")
    page.evaluate("window.scrollTo(0, 0)")
    box = page.locator("#map").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.4)
    assert page.input_value("#r-x") != "1.5"                                  # clicking the map fills x / y


def test_a_scenario_runs_with_the_authorization_question_in_plain_sight(page, server):
    page.locator("#scenarios li", has_text="t3_recover_hostile").locator("button").click()
    server.wait(lambda s: s["mission"]["status"] == "awaiting_approval", 15, "plan")
    page.wait_for_function("document.querySelectorAll('#plan li').length === 16", timeout=5000)
    assert page.locator("#source").inner_text() == "scenario" and page.locator("#approve").is_enabled()
    assert page.locator("text=Fwd 0.3 m").is_disabled()                       # manual is for between missions
    page.click("#approve")
    page.wait_for_selector("#question-card", state="visible", timeout=40000)
    assert "hostile drone" in page.inner_text("#question-text") and "(yes/no)" in page.inner_text("#question-text")
    box = page.locator("#question-card").bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= VIEWPORT["height"], "the question must not need scrolling"
    page.click("#answer-yes")
    s = server.wait(lambda s: s["mission"]["status"] in ("done", "failed"), 60, "mission end")
    assert s["mission"]["status"] == "done", s["mission"]["error"]
    page.wait_for_function("document.querySelector('#status').textContent === 'done'", timeout=5000)
    assert page.locator("#question-card").is_hidden()
    row = page.inner_text("#findings tbody tr")
    assert "hostile drone" in row and "recovered" in row and "hazardous" in row
    assert page.locator("#findings tbody img").count() == 1 and page.locator("#log li").count() >= 8
    assert page.locator("#map-dynamic line.finding.recovered").count() == 2     # the green X
    assert page.locator("#report-link").is_visible()


def test_the_search_shows_its_pattern_and_the_scouts_trail(page, server):
    page.click("#reset")
    server.wait(lambda s: s["mission"]["status"] == "idle", 8, "reset")
    page.locator("#scenarios li", has_text="t4_search_field_a").locator("button").click()
    server.wait(lambda s: s["mission"]["status"] == "awaiting_approval", 15, "t4 plan")
    page.click("#approve")
    page.wait_for_selector("#map-dynamic polyline.trail", state="attached", timeout=30000)
    server.wait(lambda s: s["mission"]["status"] in ("done", "failed"), 90, "t4 end")
    page.wait_for_selector("#map-dynamic polyline.path", state="attached", timeout=8000)
    assert page.locator("#map-dynamic circle.waypoint").count() > 5


def test_typed_missions_changes_errors_and_stop(page, server):
    page.click("#reset")
    server.wait(lambda s: s["mission"]["status"] == "idle", 8, "reset")
    page.fill("#mission-text", "A hostile drone crashed in field B. Find it and move it to the disposal zone.")
    page.click("#mission-go")
    server.wait(lambda s: s["mission"]["status"] == "awaiting_approval", 15, "typed mission plan")
    page.wait_for_function("document.querySelector('#source').textContent === 'built-in plan (no AI)'", timeout=5000)
    page.fill("#change-text", "also check field A")
    page.click("#change-go")
    page.wait_for_function("document.querySelector('#change-text').value === ''", timeout=8000)
    page.wait_for_selector("#previous", state="visible", timeout=5000)           # the old plan stays visible
    assert page.locator("#mission-text").is_disabled()                            # one mission at a time
    page.evaluate("document.getElementById('mission-text').disabled = false; document.getElementById('mission-go').disabled = false")
    page.fill("#mission-text", "another mission")
    page.click("#mission-go")
    page.wait_for_selector("#banner:not([hidden])", timeout=5000)
    assert "already" in page.inner_text("#banner")                                # the server's 409 text is shown
    page.click("#stop")                                                          # inline handler, works from any state
    server.wait(lambda s: s["mission"]["status"] == "stopped", 8, "stop")


def test_no_javascript_errors_were_logged(page):
    assert page.errors == []
