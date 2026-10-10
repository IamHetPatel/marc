"""The mission report as a page people read: GET /report.html, and runs/<mission>/report.html at the end of a mission.

/report (JSON) stays the machine-readable record; this is the same content laid out for a person, with the evidence
photos embedded, so the file stands on its own (no server, no network) and prints to PDF from any browser.
Every string from the mission (AI messages, plan text, labels) is escaped: those come from an LLM and from people.
"""
import base64
import time
from html import escape

ACTION = {
    "drone_search": "Search the field", "drone_scan": "Scan the area", "drone_mark": "Mark the spot",
    "drone_home": "Scout back to its pad", "drone_land": "Land the scout", "drone_takeoff": "Take off",
    "drone_goto": "Fly to a point", "drone_dock": "Dock on the rover", "rover_goto": "Drive",
    "rover_search": "Look around", "rover_approach": "Close in with the camera", "rover_inspect": "Inspect",
    "rover_grip": "Gripper", "rover_check_grip": "Check the grip", "rover_arm": "Arm", "rover_drive": "Drive",
    "rover_snapshot": "Take a photo", "request_authorization": "Ask the operator", "ask_human": "Ask the operator",
    "report": "Report back", "camera_locate": "Find it from above",
}
ROBOT = {"drone": "Scout", "rover": "Rover", "manager": "Mothership"}
STATUS_WORD = {"done": "Mission complete", "failed": "Mission failed", "stopped": "Stopped by the operator",
               "running": "In progress", "replanning": "In progress", "awaiting_approval": "Waiting for approval",
               "planning": "Planning", "idle": "Not started"}


def _t(ts) -> str:
    return time.strftime("%d %b %Y, %H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "–"


def _img(images: dict, image_id) -> str:
    data = images.get(image_id) if image_id else None
    if not data:
        return '<div class="noimg">no photo</div>'
    return f'<img src="data:image/jpeg;base64,{base64.b64encode(data).decode()}" alt="evidence {escape(str(image_id))}">'


def _place(t) -> str:
    """'$finding.f1' -> 'finding f1', '$zone.field_a' -> 'field A', '$home' -> 'the base'."""
    t = str(t)
    if t == "$home":
        return "the base"
    if t.startswith("$finding."):
        return "finding " + t.split(".", 1)[1]
    if t.startswith("$zone."):
        z = t.split(".", 1)[1]
        return "the disposal zone" if z == "disposal" else z.replace("field_", "field ").replace("_", " ").replace("field a", "field A").replace("field b", "field B")
    return t


def _num(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "–"


def render(report: dict, images: dict) -> str:
    m = report["mission"]
    status = m.get("status") or "idle"
    findings = report.get("findings") or []
    steps = report.get("steps") or []
    done = sum(1 for s in steps if s.get("status") == "done")
    started, finished = report.get("started"), report.get("finished")
    duration = f"{(finished - started) / 60:.1f} min" if isinstance(started, (int, float)) and isinstance(finished, (int, float)) else "–"

    rows = []
    for f in findings:
        what = (f.get("category") or f.get("label") or "unidentified").replace("_", " ")
        hazard = "–" if f.get("hazardous") is None else ("hazardous" if f["hazardous"] else "safe")
        rows.append(f"""<tr><td class="ph">{_img(images, f.get("image_id"))}</td>
          <td><b>{escape(what.capitalize())}</b><small>{escape(str(f.get("id", "")))} · by {escape(str(f.get("by", "")))}</small></td>
          <td><span class="chip {'bad' if f.get('hazardous') else ''}">{escape(hazard)}</span></td>
          <td>{'yes' if f.get('authorized') else '–'}</td>
          <td><span class="chip {'good' if f.get('status') == 'recovered' else ''}">{escape(str(f.get("status", "")))}</span></td>
          <td class="m">{_num(f.get("x"))}, {_num(f.get("y"))}</td></tr>""")
    finds_html = (f"""<table><thead><tr><th>Photo</th><th>What</th><th>Hazard</th><th>Authorized</th><th>Status</th>
        <th>Where (x, y, m)</th></tr></thead><tbody>{''.join(rows)}</tbody></table>""" if rows
                  else '<p class="muted">Nothing was found.</p>')

    step_rows = []
    for i, s in enumerate(steps, 1):
        r = s.get("result") or {}
        outcome = r.get("error") if r.get("ok") is False else (r.get("answer") or r.get("label") or "")
        args = s.get("args") or {}
        detail = _place(args["target"]) if args.get("target") else (args.get("mode") or args.get("question") or "")
        step_rows.append(f"""<li class="{escape(str(s.get('status', '')))}"><span class="n">{i}</span>
          <div><b>{escape(ACTION.get(s.get('action'), str(s.get('action'))))}</b>
          <span class="who">{escape(ROBOT.get(s.get('robot'), str(s.get('robot'))))}</span>
          {f'<span class="det">{escape(str(detail))}</span>' if detail else ''}
          <div class="why">{escape(str(s.get('why', '')))}</div>
          {f'<div class="out">{escape(str(outcome))}</div>' if outcome else ''}</div>
          <span class="st">{escape(str(s.get('status', '')))}</span></li>""")

    log_rows = "".join(f"""<li><time>{escape(time.strftime('%H:%M:%S', time.localtime(x['t'])))}</time>
        <span class="role">{escape({'human': 'Operator', 'ai': 'AI', 'system': 'System'}.get(x.get('role'), str(x.get('role'))))}</span>
        <span>{escape(str(x.get('text', '')))}</span></li>""" for x in report.get("messages") or [] if isinstance(x.get("t"), (int, float)))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mission report {escape(str(m.get('id') or ''))}</title>
<style>
  :root {{ --ink: #1d1c19; --ink2: #4a483f; --ink3: #6b685c; --line: #d8d2c2; --bg: #f6f3ea; --good: #1f7a45; --bad: #b3261e; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #e7e2d5; color: var(--ink); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  .page {{ max-width: 900px; margin: 24px auto; background: var(--bg); padding: 40px 44px; border-radius: 12px; }}
  .bar {{ max-width: 900px; margin: 18px auto 0; display: flex; gap: 10px; justify-content: flex-end; }}
  .bar button, .bar a {{ font: 600 14px system-ui, sans-serif; padding: 10px 16px; border-radius: 8px; border: 1px solid #bdb6a3;
    background: var(--bg); color: var(--ink); cursor: pointer; text-decoration: none; }}
  .bar button {{ background: var(--ink); color: var(--bg); border-color: var(--ink); }}
  header .brand {{ font: 600 12px ui-monospace, Menlo, monospace; letter-spacing: .14em; text-transform: uppercase; color: var(--ink3); }}
  h1 {{ font-size: 26px; line-height: 1.2; margin: 6px 0 4px; }}
  h2 {{ font-size: 17px; margin: 30px 0 10px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }}
  .verdict {{ display: inline-block; margin-top: 8px; padding: 4px 12px; border-radius: 99px; font-weight: 600; font-size: 14px;
    border: 1px solid var(--ink3); }}
  .verdict.done {{ color: var(--good); border-color: var(--good); }} .verdict.failed, .verdict.stopped {{ color: var(--bad); border-color: var(--bad); }}
  .facts {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 20px; }}
  .facts div {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }}
  .facts b {{ display: block; font-size: 19px; }} .facts span {{ font-size: 12px; color: var(--ink3); }}
  .muted {{ color: var(--ink3); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ text-align: left; font: 600 11px ui-monospace, Menlo, monospace; letter-spacing: .08em; text-transform: uppercase; color: var(--ink3);
    padding: 6px 8px; border-bottom: 1px solid var(--line); }}
  td {{ padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: middle; }}
  td small {{ display: block; color: var(--ink3); font-size: 12px; }} td.m {{ font-family: ui-monospace, Menlo, monospace; }}
  td.ph img, .noimg {{ width: 150px; height: 94px; object-fit: cover; border-radius: 6px; border: 1px solid var(--line); display: grid; place-items: center; color: var(--ink3); font-size: 12px; }}
  .chip {{ font-size: 12px; padding: 2px 9px; border-radius: 99px; border: 1px solid var(--ink3); white-space: nowrap; }}
  .chip.bad {{ color: var(--bad); border-color: var(--bad); }} .chip.good {{ color: var(--good); border-color: var(--good); }}
  ol.steps {{ list-style: none; padding: 0; margin: 0; }}
  ol.steps li {{ display: grid; grid-template-columns: 28px 1fr auto; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--line); }}
  ol.steps .n {{ font: 600 12px ui-monospace, Menlo, monospace; color: var(--ink3); padding-top: 2px; }}
  ol.steps .who, ol.steps .det {{ font-size: 12px; color: var(--ink3); margin-left: 8px; }}
  ol.steps .why {{ color: var(--ink2); font-size: 13.5px; }} ol.steps .out {{ font-size: 13px; color: var(--ink2); font-style: italic; }}
  ol.steps .st {{ font-size: 12px; color: var(--ink3); }} ol.steps li.failed .st {{ color: var(--bad); }} ol.steps li.done .st {{ color: var(--good); }}
  ul.log {{ list-style: none; padding: 0; margin: 0; font-size: 13.5px; }}
  ul.log li {{ display: grid; grid-template-columns: 70px 72px 1fr; gap: 8px; padding: 4px 0; }}
  ul.log time {{ font-family: ui-monospace, Menlo, monospace; color: var(--ink3); }} ul.log .role {{ color: var(--ink3); }}
  footer {{ margin-top: 30px; font-size: 12px; color: var(--ink3); }}
  @media print {{ body {{ background: #fff; }} .bar {{ display: none; }} .page {{ margin: 0; padding: 0; max-width: none; border-radius: 0; background: #fff; }}
    tr, li {{ break-inside: avoid; }} }}
  @media (max-width: 640px) {{ .page {{ padding: 22px 18px; }} .facts {{ grid-template-columns: repeat(2, 1fr); }} td.ph img, .noimg {{ width: 96px; height: 60px; }} }}
</style></head>
<body>
<div class="bar"><a href="/report" target="_blank" rel="noopener">JSON</a><button onclick="window.print()">Save as PDF</button></div>
<main class="page">
  <header>
    <div class="brand">FleetPilot · mission report</div>
    <h1>{escape(str(m.get('text') or 'Mission'))}</h1>
    <span class="verdict {escape(status)}">{escape(STATUS_WORD.get(status, status))}</span>
    {f'<p class="muted">{escape(str(m["error"]))}</p>' if m.get("error") else ''}
    <div class="facts">
      <div><b>{len(findings)}</b><span>found</span></div>
      <div><b>{sum(1 for f in findings if f.get('status') == 'recovered')}</b><span>recovered</span></div>
      <div><b>{done} / {len(steps)}</b><span>steps done</span></div>
      <div><b>{escape(duration)}</b><span>duration</span></div>
    </div>
  </header>
  <h2>What was found</h2>
  {finds_html}
  <h2>What the robots did</h2>
  <ol class="steps">{''.join(step_rows) or '<li class="muted">No steps.</li>'}</ol>
  <h2>Decisions and messages</h2>
  <ul class="log">{log_rows or '<li class="muted">None.</li>'}</ul>
  <footer>Mission {escape(str(m.get('id') or ''))} · plan from {escape(str(m.get('source') or ''))} ·
    started {escape(_t(started))} · finished {escape(_t(finished))} · positions in metres, Lighthouse frame</footer>
</main>
</body></html>"""
