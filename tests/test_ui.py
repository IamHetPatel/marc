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
        pg.wait_for_function("document.querySelectorAll('#start-list li').length >= 5", timeout=8000)
        pg.wait_for_function("document.querySelector('#light-rover').classList.contains('on')", timeout=8000)
        pg.errors = errors
        pg.browser = browser
        yield pg
        browser.close()


def view(page, name):
    page.click(f"[data-view={name}]")
    page.wait_for_selector(f"#view-{name}", state="visible")


def start_over(page, server):
    """Back to an idle, empty screen; 'Start over' is disabled when there is nothing to start over from."""
    if server.state()["mission"]["id"] is not None:
        page.click("#reset")                                                                  # waits until the page has caught up
    server.wait(lambda s: s["mission"]["status"] == "idle", 8, "idle")
    page.wait_for_function("document.querySelectorAll('#plan li').length === 0", timeout=4000)
    view(page, "mission")
    page.wait_for_selector("#start-panel", state="visible", timeout=4000)


def visible_in(page, selector, container):
    """Is `selector` fully inside the scrolling `container` right now?"""
    return page.evaluate("""([s, c]) => { const a = document.querySelector(s).getBoundingClientRect();
        const b = document.querySelector(c).getBoundingClientRect(); return a.top >= b.top - 1 && a.bottom <= b.bottom + 1; }""",
                         [selector, container])


# ---------------------------------------------------------------- the page itself
def test_the_boot_screen_goes_away_and_the_field_is_drawn(page, server):
    assert page.url.endswith("/ui/")                                          # GET / redirects
    page.wait_for_function("!document.getElementById('boot') || document.getElementById('boot').classList.contains('gone')")
    assert page.locator("#light-drone.on").count() == 1 and page.locator("#light-target.on").count() == 1
    assert page.locator("#mk-rover .rover-body").count() == 1 and page.locator("#mk-rover").evaluate("e => e.style.display") == ""
    assert page.locator("#mk-target .target-ring").count() == 1               # the target's ground truth
    assert page.locator("#mk-scout").count() == 1 and page.locator("#map .zone.disposal").count() == 1
    assert page.locator("#approve").is_disabled() and page.locator("#approval-card").is_hidden()
    assert page.inner_text("#rover-x") != "–" and page.inner_text("#scout-batt").endswith("%")   # the status strip is live


def test_it_is_an_app_not_a_long_page(page):
    """At laptop size nothing scrolls except the panels meant to; no horizontal overflow; STOP is on screen."""
    m = page.evaluate("({sh: document.documentElement.scrollHeight, ih: innerHeight, sw: document.documentElement.scrollWidth, iw: innerWidth})")
    assert m["sh"] <= m["ih"] + 1, f"the page scrolls: {m}"
    assert m["sw"] <= m["iw"], f"horizontal overflow: {m}"
    box = page.locator("#stop").bounding_box()
    assert 0 <= box["y"] and box["y"] + box["height"] <= 70 and box["x"] + box["width"] <= VIEWPORT["width"]
    assert page.locator("#stop").evaluate("e => getComputedStyle(e).position") != "fixed"      # it lives in the bar


def test_the_theme_toggle_flips_and_remembers(page):
    start = page.evaluate("document.documentElement.dataset.theme")
    bg0 = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.click("#theme")
    page.wait_for_timeout(450)
    now = page.evaluate("document.documentElement.dataset.theme")
    assert now != start and page.evaluate("localStorage.getItem('fp-theme')") == now
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") != bg0
    page.reload()
    page.wait_for_selector("#map-dynamic", state="attached")
    assert page.evaluate("document.documentElement.dataset.theme") == now                  # no flash back
    page.click("#theme")
    page.wait_for_timeout(450)


def test_the_sections_switch_and_the_page_remembers(page, server):
    """Four sections, one visible at a time; the start screen sells the product with the use cases."""
    assert page.locator("#start-panel").is_visible() and page.locator("#start-list .usecase").count() >= 5
    assert "Send robots" in page.inner_text("#start-panel")
    assert page.locator("#phases li").count() == 8                                          # the story, before any mission
    for name in ("plan", "fleet", "evidence", "mission"):
        view(page, name)
        assert page.get_attribute(f"[data-view={name}]", "aria-selected") == "true"
        assert sum(page.locator(f"#view-{v}").is_visible() for v in ("mission", "plan", "fleet", "evidence")) == 1
    view(page, "fleet")
    page.reload()
    page.wait_for_selector("#view-fleet", state="visible", timeout=8000)                    # remembered
    view(page, "mission")


def test_the_keyboard_shortcuts(page):
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "mission-text"
    page.keyboard.press("Escape")
    page.evaluate("document.activeElement.blur()")


# ---------------------------------------------------------------- operating it
def test_mentor_toggle_and_manual_control(page, server):
    page.check("#mentor")
    server.wait(lambda s: s["mentor_present"], 8, "mentor")
    view(page, "fleet")
    page.click("text=Fwd 0.3 m")
    s = server.wait(lambda s: (s["manual"] or {}).get("status") in ("done", "failed"), 15, "manual drive")
    assert s["manual"]["action"] == "rover_drive" and s["manual"]["status"] == "done"
    page.wait_for_function("document.querySelector('#manual-last').textContent.includes('rover_drive')", timeout=5000)
    page.click("text=Snapshot")
    server.wait(lambda s: s["last_image_id"], 10, "snapshot")
    view(page, "mission")
    page.wait_for_function("document.querySelector('#camera').src.includes('/images/img_')", timeout=5000)
    assert page.locator("#camera").evaluate("i => i.complete && i.naturalWidth > 0")
    box = page.locator("#map").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.4)
    page.wait_for_function("document.getElementById('readout').textContent.startsWith('x ') && !document.getElementById('readout').textContent.includes('–')")
    page.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.4)
    assert page.input_value("#r-x") != "1.5"                                                # clicking the map fills x / y
    page.click("#camera-card")                                                              # the camera feed opens large
    page.wait_for_selector("#lightbox", state="visible")
    page.keyboard.press("Escape")
    page.wait_for_selector("#lightbox", state="hidden")


def test_a_scenario_runs_with_the_authorization_question_in_plain_sight(page, server):
    page.click("[data-scenario=t3_recover_hostile]")
    server.wait(lambda s: s["mission"]["status"] == "awaiting_approval", 15, "plan")
    page.wait_for_function("document.querySelectorAll('#plan li').length === 16", timeout=5000)
    assert page.locator("#source").inner_text().lower() == "scenario" and page.locator("#approve").is_enabled()
    assert page.locator("#approval-card").is_visible() and page.locator("#plan-empty").is_hidden()
    assert page.locator("text=Fwd 0.3 m").is_disabled()                                     # manual is for between missions
    page.click("#approve")
    page.wait_for_selector("#question-card", state="visible", timeout=40000)
    text = page.inner_text("#question-text")
    assert "hostile drone" in text and "(yes/no)" in text
    box = page.locator("#question-card").bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= VIEWPORT["height"], "the question must not need scrolling"
    assert page.locator("#approval-card").is_hidden()
    assert page.inner_text("#phases li.active b") == "Authorize"                             # the story at a glance
    assert "decision" in page.inner_text("#now-title").lower()
    assert page.locator("#plan li.done").count() >= 5
    view(page, "plan")
    page.wait_for_function("""() => { const a = document.querySelector('#plan li.current').getBoundingClientRect();
        const b = document.querySelector('#plan-scroll').getBoundingClientRect(); return a.top >= b.top - 1 && a.bottom <= b.bottom + 1; }""",
                           timeout=4000)                                                   # the working step is in view
    view(page, "mission")
    page.click("#answer-yes")
    s = server.wait(lambda s: s["mission"]["status"] in ("done", "failed"), 60, "mission end")
    assert s["mission"]["status"] == "done", s["mission"]["error"]
    page.wait_for_function("document.querySelector('#status').textContent === 'done'", timeout=5000)
    assert page.locator("#question-card").is_hidden() and page.locator("#result-card").is_visible()
    assert "complete" in page.inner_text("#result-title").lower() and page.locator("#report-link").is_visible()
    assert page.locator("#plan li.done").count() == 16 and page.inner_text("#plan-count").strip() == "16 / 16"
    assert page.locator("#plan-bar").evaluate("e => e.style.width") == "100%"
    assert page.locator("#phases li").count() == page.locator("#phases li.done").count()   # every phase ticked off
    assert page.locator("#map-dynamic .finding.recovered").count() == 1                      # the green X
    assert "complete" in page.inner_text("#ticker-text").lower()
    page.click("#to-evidence")
    page.wait_for_selector("#view-evidence", state="visible")
    row = page.inner_text("#findings tbody tr").lower()
    assert "hostile drone" in row and "recovered" in row and "hazardous" in row
    assert page.locator("#findings tbody img").count() == 1 and page.inner_text("#findings-count") == "1"
    assert page.locator("#log li").count() >= 8 and page.locator("#report-link-ev").is_visible()
    view(page, "mission")


def test_the_search_shows_its_pattern_and_the_scouts_trail(page, server):
    start_over(page, server)                                                                   # also proves old rows are gone
    assert page.locator("#start-panel").is_visible() and page.locator("#now-card").is_hidden()
    page.click("[data-scenario=t4_search_field_a]")
    server.wait(lambda s: s["mission"]["status"] == "awaiting_approval", 15, "t4 plan")
    page.click("#approve")
    page.wait_for_selector("#map-dynamic polyline.trail", state="attached", timeout=30000)
    page.wait_for_function("document.querySelector('#mk-scout').classList.contains('flying')", timeout=30000)
    server.wait(lambda s: s["mission"]["status"] in ("done", "failed"), 90, "t4 end")
    page.wait_for_selector("#map-dynamic polyline.path", state="attached", timeout=8000)
    assert page.locator("#map-dynamic circle.waypoint").count() > 5


def test_the_robots_glide_instead_of_jumping(page, server):
    """The marker moves through intermediate positions between polls (a CSS transition), it does not teleport."""
    start_over(page, server)
    view(page, "fleet")
    page.click("#r-goto")
    view(page, "mission")                                                                   # watch it on the map
    seen = set()
    t0 = time.time()
    while time.time() - t0 < 6:
        seen.add(page.evaluate("getComputedStyle(document.getElementById('mk-rover')).transform"))
        time.sleep(0.05)
    server.wait(lambda s: (s["manual"] or {}).get("status") in ("done", "failed"), 20, "manual goto")
    assert len(seen) > 6, f"only {len(seen)} distinct positions were rendered"


def test_typed_missions_changes_errors_and_stop(page, server):
    start_over(page, server)
    page.fill("#mission-text", "A hostile drone crashed in field B. Find it and move it to the disposal zone.")
    page.click("#mission-go")
    server.wait(lambda s: s["mission"]["status"] == "awaiting_approval", 15, "typed mission plan")
    page.wait_for_function("document.querySelector('#source').textContent === 'built-in plan (no AI)'", timeout=5000)
    assert "field B" in page.inner_text("#plan li >> nth=0") and page.inner_text("#mission-title").startswith("A hostile drone")
    page.click("#adjust")
    assert page.evaluate("document.activeElement.id") == "change-text"
    page.fill("#change-text", "also check field A")
    page.click("#change-go")
    page.wait_for_function("document.querySelector('#change-text').value === ''", timeout=8000)
    view(page, "plan")
    page.wait_for_selector("#previous", state="visible", timeout=5000)                       # the old plan stays visible
    view(page, "mission")
    assert page.locator("#mission-form").is_hidden() and page.locator("#mission-text").is_disabled()   # one mission at a time
    assert page.locator("#change-form").is_visible()
    # the form is hidden while a mission is live, so provoke the server's 409 by submitting it programmatically
    page.evaluate("document.getElementById('mission-text').value = 'another mission'; document.getElementById('mission-form').requestSubmit()")
    page.wait_for_selector("#banner:not([hidden])", timeout=5000)
    assert "already" in page.inner_text("#banner")                                           # the server's 409 text is shown
    page.click("#stop")                                                                      # inline handler, works from any state
    server.wait(lambda s: s["mission"]["status"] == "stopped", 8, "stop")
    page.wait_for_function("document.querySelector('#result-title').textContent === 'Stopped'", timeout=5000)


def test_a_phone_sized_screen_stacks_without_sideways_scrolling(page, server):
    ctx = page.browser.new_context(viewport={"width": 390, "height": 844})
    pg = ctx.new_page()
    pg.goto(server.base + "/")
    pg.wait_for_selector("#map-dynamic", state="attached", timeout=8000)
    m = pg.evaluate("({sw: document.documentElement.scrollWidth, iw: innerWidth})")
    assert m["sw"] <= m["iw"], m
    box = pg.locator("#stop").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390
    assert pg.locator("#map").bounding_box()["width"] > 300
    # what needs a human comes before the map on a phone: raise a question and check its position
    pg.evaluate("window.fleetpilot.state.s.mission.pending_question = 'Authorize?'; window.fleetpilot.render()")
    q, m = pg.locator("#question-card").bounding_box(), pg.locator("#map-card").bounding_box()
    assert q["y"] < m["y"], (q, m)
    ctx.close()


def test_no_javascript_errors_were_logged(page):
    assert page.errors == []
