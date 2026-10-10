/* FleetPilot mothership dashboard. Plain JS, no framework, no CDN.
 *
 * Reads GET /state every 500 ms and draws it; every control is one POST back to the mothership.
 * Everything that reaches the page from the server (plan text, AI messages, errors) is inserted with
 * textContent / createElement, never innerHTML: those strings come from an LLM and from people.
 * The STOP button lives in index.html with its own inline handler so it works even if this file breaks.
 */
"use strict";
(function () {
  var POLL_MS = 500;
  var IDLE = ["idle", "done", "failed", "stopped"];
  var LIVE = ["planning", "awaiting_approval", "running", "replanning"];
  var SVGNS = "http://www.w3.org/2000/svg";
  var TRAIL_MAX = 300;

  var $ = function (id) { return document.getElementById(id); };
  var S = {                      // everything the page remembers
    s: null, arena: null, scenarios: [], trail: [], trailMission: null, pick: null,
    busy: new Set(), lost: 0, cache: {}, bannerTimer: null
  };

  /* ------------------------------------------------------------------ DOM helpers */
  function h(tag, props) {
    var e = document.createElement(tag);
    var p = props || {};
    Object.keys(p).forEach(function (k) {
      var v = p[k];
      if (v === false || v == null) return;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.indexOf("on") === 0) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) {
      var kid = arguments[i];
      if (kid == null || kid === false) continue;
      e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return e;
  }
  function sv(tag, attrs) {
    var e = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k]; else e.setAttribute(k, attrs[k]);
    });
    for (var i = 2; i < arguments.length; i++) if (arguments[i]) e.append(arguments[i]);
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function changed(key, value) {          // true when `value` differs from what was last rendered under `key`
    var j = JSON.stringify(value);
    if (S.cache[key] === j) return false;
    S.cache[key] = j;
    return true;
  }
  function n2(v) { return typeof v === "number" ? v.toFixed(2) : "?"; }
  function hms(t) { return new Date(t * 1000).toTimeString().slice(0, 8); }
  function isIdle() { return !!S.s && IDLE.indexOf(S.s.mission.status) >= 0; }
  function status() { return S.s ? S.s.mission.status : "idle"; }

  /* ------------------------------------------------------------------ talking to the mothership */
  function errText(data, code) {
    var d = data && data.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map(function (x) { return x.msg || JSON.stringify(x); }).join("; ");
    return "HTTP " + code;
  }
  function api(method, path, body) {
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 4000);
    var opts = { method: method, signal: ctl.signal };
    if (body !== undefined) { opts.headers = { "Content-Type": "application/json" }; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (data) {
        return { ok: r.ok, code: r.status, data: data, error: r.ok ? null : errText(data, r.status) };
      });
    }, function () {
      return { ok: false, code: 0, data: null, error: "no connection to the mothership" };
    }).then(function (res) { clearTimeout(timer); return res; });
  }
  function banner(text, kind, sticky) {
    var b = $("banner");
    b.textContent = text;
    b.className = "banner" + (kind ? " " + kind : "");
    b.hidden = false;
    clearTimeout(S.bannerTimer);
    if (!sticky) S.bannerTimer = setTimeout(function () { b.hidden = true; }, 7000);
  }
  /* Run a request for a button: disabled while in flight, server error text shown on 409 / 422. */
  function act(el, fn) {
    if (!el) return;
    S.busy.add(el);
    render();
    return Promise.resolve().then(fn).then(function (res) {
      if (res && !res.ok) banner(res.error);
      else $("banner").hidden = true;
      return res;
    }).then(function (res) { S.busy.delete(el); poll(true); return res; },
            function (e) { S.busy.delete(el); banner(String(e)); });
  }
  function wire(id, fn) { var el = $(id); if (el) el.addEventListener("click", function (ev) { ev.preventDefault(); act(el, fn); }); }
  function setEnabled(el, on) { if (el) el.disabled = !on || S.busy.has(el); }

  /* ------------------------------------------------------------------ polling */
  var polling = false;
  function poll(once) {
    if (polling && once) return;
    polling = true;
    api("GET", "/state").then(function (r) {
      polling = false;
      if (r.ok) {
        if (S.lost >= 2) $("banner").hidden = true;
        S.lost = 0;
        S.s = r.data;
        updateTrail();
        render();
      } else if (++S.lost >= 2) {
        banner("Connection to the mothership lost. Retrying...", "", true);
      }
      if (!once) setTimeout(poll, POLL_MS);
    });
  }
  function updateTrail() {
    var s = S.s;
    if (S.trailMission !== s.mission.id) { S.trail = []; S.trailMission = s.mission.id; }
    var d = s.robots.drone;
    if (d && d.online && d.state === "flying" && d.pose && typeof d.pose.x === "number") {
      var last = S.trail[S.trail.length - 1];
      if (!last || Math.hypot(last[0] - d.pose.x, last[1] - d.pose.y) > 0.03) {
        S.trail.push([d.pose.x, d.pose.y]);
        if (S.trail.length > TRAIL_MAX) S.trail.shift();
      }
    }
  }
  function loadArena() {
    api("GET", "/arena").then(function (r) {
      if (r.ok) { S.arena = r.data; buildMap(); render(); } else setTimeout(loadArena, 1500);
    });
  }
  function loadScenarios() {
    api("GET", "/scenarios").then(function (r) {
      if (r.ok) { S.scenarios = r.data; S.cache.scenarios = null; render(); } else setTimeout(loadScenarios, 1500);
    });
  }

  /* ------------------------------------------------------------------ render */
  function render() {
    if (!S.s) return;
    try {
      renderBar(); renderMission(); renderQuestion(); renderScenarios(); renderManual();
      renderCamera(); renderFindings(); renderLog(); renderMap();
    } catch (e) {
      banner("Dashboard error: " + e.message + " (STOP still works)", "", true);
      if (window.console) console.error(e);
    }
  }

  function renderBar() {
    var s = S.s, m = s.mission;
    var st = $("status");
    st.textContent = m.status.replace("_", " ");
    st.className = "pill s-" + m.status;
    var src = $("source");
    src.hidden = !m.id;
    src.textContent = m.source === "fallback" ? "built-in plan (no AI)" : m.source === "scenario" ? "scenario"
      : m.source === "manual" ? "submitted plan" : "AI plan";
    [["rover", "rover"], ["drone", "scout"], ["target", "target"]].forEach(function (p) {
      var r = s.robots[p[0]];
      $("light-" + p[0]).className = "light " + (r && r.online ? "on" : "off");
    });
    var mt = s.metrics || {}, parts = [];
    if (mt.rover_rtt_ms != null) parts.push("rover " + mt.rover_rtt_ms + " ms");
    if (mt.drone_rtt_ms != null) parts.push("scout " + mt.drone_rtt_ms + " ms");
    if (mt.llm_latency_ms_last != null) parts.push("AI " + mt.llm_latency_ms_last + " ms");
    $("rtt").textContent = parts.join(" · ");
    var mentor = $("mentor");
    if (document.activeElement !== mentor) mentor.checked = !!s.mentor_present;
    $("calibration").textContent = S.arena && !S.arena.calibrated ? "placeholder geometry, not calibrated yet" : "";
  }

  function summarize(step) {
    var r = step.result;
    if (!r) return step.status === "running" ? "running..." : "";
    if (r.ok === false) return "✗ " + (r.error || "failed");
    var p = [];
    if (r.arrived !== undefined) p.push("arrived (±" + r.error_m + " m)");
    if (r.waypoints_flown !== undefined) p.push(r.waypoints_flown + " waypoints");
    if (r.findings) p.push(r.findings.length ? r.findings.map(function (f) { return f.id; }).join(", ") + " found" : "nothing found");
    if (r.found !== undefined) p.push(r.found ? "found it" : "not found");
    if (r.in_reach !== undefined) p.push("in reach after " + r.steps + " moves");
    if (r.label) p.push(r.label + (r.category ? " · " + r.category.replace(/_/g, " ") : "") +
      (r.hazardous ? " · HAZARDOUS" : ""));
    if (r.holding !== undefined) p.push(r.holding ? "holding it" : "not holding it");
    if (r.authorized !== undefined) p.push(r.authorized ? "authorized" : "declined");
    if (step.action === "drone_mark" && r.finding_id) p.push("marked " + r.finding_id);
    if (r.answer && step.action === "ask_human") p.push("answer: " + r.answer);
    return "✓ " + p.join(" · ");
  }
  function planItems(steps, current) {
    return steps.map(function (st) {
      return h("li", { class: st.status + (st.id === current ? " current" : "") },
        h("span", { class: "dot" }), h("span", { class: "sid", text: st.id }),
        h("span", { class: "act" }, st.action, h("span", { class: "robot", text: st.robot })),
        st.why ? h("span", { class: "why", text: st.why }) : null,
        summarize(st) ? h("span", { class: "res", text: summarize(st) }) : null);
    });
  }
  function renderMission() {
    var s = S.s, m = s.mission, st = m.status, idle = IDLE.indexOf(st) >= 0;
    var reason = $("reason");
    var text = m.status === "failed" && m.error ? "Mission failed: " + m.error : m.reason;
    reason.hidden = !text;
    reason.textContent = text || "";
    reason.className = "reason" + (m.status === "failed" ? " bad" : "");
    setEnabled($("approve"), st === "awaiting_approval");
    setEnabled($("mission-go"), idle);
    setEnabled($("mission-text"), idle);
    setEnabled($("reset"), st !== "running" && st !== "replanning");
    setEnabled($("change-go"), LIVE.indexOf(st) >= 0);
    $("report-link").hidden = !(m.id && idle && st !== "idle");
    if (changed("plan", [s.plan, s.current_step, m.plan_version])) {
      var ol = $("plan"); clear(ol);
      planItems(s.plan, s.current_step).forEach(function (li) { ol.append(li); });
    }
    var prev = s.previous_plan;
    $("previous").hidden = !prev;
    if (prev && changed("prev", prev)) {
      var po = $("previous-plan"); clear(po);
      planItems(prev, null).forEach(function (li) { po.append(li); });
    }
  }

  function renderQuestion() {
    var q = S.s.mission.pending_question;
    $("question-card").hidden = !q;
    if (q) $("question-text").textContent = q;
    setEnabled($("answer-yes"), !!q); setEnabled($("answer-no"), !!q);
  }

  function renderScenarios() {
    if (!changed("scenarios", S.scenarios)) { updateScenarioButtons(); return; }
    var ul = $("scenarios"); clear(ul);
    S.scenarios.forEach(function (sc) {
      var b = h("button", { type: "button", "data-scenario": sc.name, text: "Load" });
      b.addEventListener("click", function () {
        act(b, function () { return api("POST", "/scenarios/" + encodeURIComponent(sc.name) + "/load"); });
      });
      ul.append(h("li", null, h("span", { class: "t" }, sc.title, h("small", { text: sc.name + (sc.has_plan ? " · scripted plan" : " · AI plans it") })), b));
    });
    updateScenarioButtons();
  }
  function updateScenarioButtons() {
    document.querySelectorAll("[data-scenario]").forEach(function (b) { setEnabled(b, isIdle()); });
  }

  function renderManual() {
    var idle = isIdle();
    document.querySelectorAll("#manual-card button").forEach(function (b) { setEnabled(b, idle); });
    $("manual-hint").textContent = idle ? "" : "only between missions";
    var m = S.s.manual, box = $("manual-last");
    if (!m) { box.textContent = ""; box.className = "manual-last muted"; return; }
    var args = Object.keys(m.args || {}).map(function (k) { return k + "=" + m.args[k]; }).join(" ");
    var res = m.status === "running" ? "running..." : m.status === "done" ? "done" :
      "failed: " + ((m.result && m.result.error) || "");
    box.textContent = "last: " + m.action + (args ? " " + args : "") + " → " + res;
    box.className = "manual-last " + (m.status === "failed" ? "bad" : m.status === "done" ? "ok" : "muted");
  }

  function renderCamera() {
    var id = S.s.last_image_id, img = $("camera");
    $("camera-id").textContent = id || "";
    $("camera-empty").hidden = !!id;
    img.hidden = !id;
    if (id && S.cache.cam !== id) { S.cache.cam = id; img.src = "/images/" + encodeURIComponent(id) + ".jpg"; }
  }

  function renderFindings() {
    var f = S.s.findings;
    $("findings-empty").hidden = f.length > 0;
    if (!changed("findings", f)) return;
    var tb = document.querySelector("#findings tbody"); clear(tb);
    f.forEach(function (x) {
      var cls = x.status === "recovered" ? "recovered" : x.status === "skipped" ? "skipped" : x.status === "inspected" ? "inspected" : "";
      tb.append(h("tr", null,
        h("td", { text: x.id }), h("td", { text: n2(x.x) }), h("td", { text: n2(x.y) }),
        h("td", { text: x.category ? x.category.replace(/_/g, " ") : (x.label || "—") }),
        h("td", null, x.hazardous == null ? "—" : h("span", { class: "badge" + (x.hazardous ? " bad" : ""), text: x.hazardous ? "hazardous" : "no" })),
        h("td", { text: x.authorized ? "yes" : "no" }),
        h("td", null, h("span", { class: "badge " + cls, text: x.status })),
        h("td", null, x.image_id ? h("img", { src: "/images/" + encodeURIComponent(x.image_id) + ".jpg", alt: "evidence for " + x.id }) : "")));
    });
  }

  function renderLog() {
    var msgs = S.s.messages;
    if (!changed("log", msgs.length + ":" + (msgs.length ? msgs[msgs.length - 1].text : ""))) return;
    var ul = $("log");
    var atBottom = ul.scrollHeight - ul.scrollTop - ul.clientHeight < 40;
    clear(ul);
    msgs.forEach(function (m) {
      ul.append(h("li", { class: m.role }, h("time", { text: hms(m.t) }), h("b", { text: m.role }), h("span", { text: m.text })));
    });
    if (atBottom) ul.scrollTop = ul.scrollHeight;
  }

  /* ------------------------------------------------------------------ map */
  var PAD = 34, SCALE = 190;
  function geo() {
    var b = S.arena.bounds;
    return { b: b, w: (b.x1 - b.x0) * SCALE + 2 * PAD, h: (b.y1 - b.y0) * SCALE + 2 * PAD };
  }
  function sx(x) { return PAD + (x - S.arena.bounds.x0) * SCALE; }
  function sy(y) { return PAD + (S.arena.bounds.y1 - y) * SCALE; }
  function buildMap() {
    var g = geo(), svg = $("map");
    clear(svg);
    svg.setAttribute("viewBox", "0 0 " + g.w + " " + g.h);
    var st = sv("g", { id: "map-static" }), b = g.b, a = S.arena;
    st.append(sv("rect", { class: "bounds", x: sx(b.x0), y: sy(b.y1), width: (b.x1 - b.x0) * SCALE, height: (b.y1 - b.y0) * SCALE }));
    for (var x = Math.ceil(b.x0); x <= b.x1; x++) {
      st.append(sv("line", { class: "grid", x1: sx(x), y1: sy(b.y0), x2: sx(x), y2: sy(b.y1) }));
      st.append(sv("text", { class: "tick", x: sx(x) - 4, y: sy(b.y0) + 16, text: x + " m" }));
    }
    for (var y = Math.ceil(b.y0); y <= b.y1; y++) {
      st.append(sv("line", { class: "grid", x1: sx(b.x0), y1: sy(y), x2: sx(b.x1), y2: sy(y) }));
      st.append(sv("text", { class: "tick", x: 4, y: sy(y) + 4, text: y + " m" }));
    }
    var c = a.drone_cage;
    st.append(sv("rect", { class: "cage", x: sx(c.x0), y: sy(c.y1), width: (c.x1 - c.x0) * SCALE, height: (c.y1 - c.y0) * SCALE }));
    st.append(sv("text", { class: "tick", x: sx(c.x0) + 6, y: sy(c.y1) - 6, text: "flight cage" }));
    Object.keys(a.zones).forEach(function (name) {
      var z = a.zones[name];
      if (name === "test_zone") return;                      // identical to the cage
      st.append(sv("rect", { class: "zone " + name, x: sx(z.x0), y: sy(z.y1), width: (z.x1 - z.x0) * SCALE, height: (z.y1 - z.y0) * SCALE }));
      st.append(sv("text", { class: "zone-label", x: sx(z.x0) + 8, y: sy(z.y1) + 18, text: name.replace(/_/g, " ") }));
    });
    var hm = a.home;
    st.append(sv("line", { class: "home", x1: sx(hm.x) - 7, y1: sy(hm.y), x2: sx(hm.x) + 7, y2: sy(hm.y) }));
    st.append(sv("line", { class: "home", x1: sx(hm.x), y1: sy(hm.y) - 7, x2: sx(hm.x), y2: sy(hm.y) + 7 }));
    svg.append(st, sv("g", { id: "map-dynamic" }));
    svg.addEventListener("click", function (ev) {
      var pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
      var p = pt.matrixTransform(svg.getScreenCTM().inverse());
      var x = (p.x - PAD) / SCALE + S.arena.bounds.x0, y = S.arena.bounds.y1 - (p.y - PAD) / SCALE;
      S.pick = { x: x, y: y };
      ["d-x", "r-x"].forEach(function (id) { $(id).value = x.toFixed(2); });
      ["d-y", "r-y"].forEach(function (id) { $(id).value = y.toFixed(2); });
      renderMap();
    });
  }
  function renderMap() {
    if (!S.arena || !S.s || !$("map-dynamic")) return;
    var s = S.s, dyn = $("map-dynamic");
    clear(dyn);
    // the planned search pattern, from the latest drone_search result
    var search = null;
    s.plan.forEach(function (st) { if (st.action === "drone_search" && st.result && st.result.search_path) search = st.result.search_path; });
    if (search && search.length) {
      dyn.append(sv("polyline", { class: "path", points: search.map(function (p) { return sx(p[0]) + "," + sy(p[1]); }).join(" ") }));
      search.forEach(function (p) { dyn.append(sv("circle", { class: "waypoint", cx: sx(p[0]), cy: sy(p[1]), r: 2.5 })); });
    }
    if (S.trail.length > 1) dyn.append(sv("polyline", { class: "trail", points: S.trail.map(function (p) { return sx(p[0]) + "," + sy(p[1]); }).join(" ") }));
    var t = s.robots.target;
    if (t && t.online && t.pose && typeof t.pose.x === "number") {
      dyn.append(sv("circle", { class: "truth", cx: sx(t.pose.x), cy: sy(t.pose.y), r: 14 }));
      dyn.append(sv("text", { class: "truth-label", x: sx(t.pose.x) + 17, y: sy(t.pose.y) + 4, text: "truth" }));
    }
    s.findings.forEach(function (f) {
      var cls = "finding" + (f.status === "recovered" ? " recovered" : f.status === "skipped" ? " skipped" : f.hazardous ? " hazardous" : "");
      var x = sx(f.x), y = sy(f.y);
      dyn.append(sv("line", { class: cls, x1: x - 7, y1: y - 7, x2: x + 7, y2: y + 7 }));
      dyn.append(sv("line", { class: cls, x1: x - 7, y1: y + 7, x2: x + 7, y2: y - 7 }));
      dyn.append(sv("text", { class: "tag", x: x + 11, y: y - 12, text: f.id + (f.category ? " " + f.category.replace(/_/g, " ") : "") }));
    });
    var d = s.robots.drone;
    if (d && d.online && d.pose && typeof d.pose.x === "number") {
      var flying = d.state === "flying";
      dyn.append(sv("circle", { class: flying ? "scout-flying" : "scout-landed", cx: sx(d.pose.x), cy: sy(d.pose.y), r: 8 }));
      dyn.append(sv("text", { class: "tag", x: sx(d.pose.x) + 12, y: sy(d.pose.y) + 20,
        text: "scout" + (flying ? " z " + n2(d.pose.z) : " (landed)") }));
    }
    var r = s.robots.rover;
    if (r && r.online && r.pose && typeof r.pose.x === "number") {
      var g = sv("g", { transform: "translate(" + sx(r.pose.x) + "," + sy(r.pose.y) + ") rotate(" + (-(r.pose.yaw || 0)) + ")" });
      g.append(sv("polygon", { class: "rover", points: "15,0 -11,-10 -11,10" }));
      dyn.append(g);
      dyn.append(sv("text", { class: "tag", x: sx(r.pose.x) + 14, y: sy(r.pose.y) + 22, text: "rover · " + (r.pose_source || "") }));
    }
    if (S.pick) {
      var px = sx(S.pick.x), py = sy(S.pick.y);
      dyn.append(sv("line", { class: "pick", x1: px - 6, y1: py, x2: px + 6, y2: py }));
      dyn.append(sv("line", { class: "pick", x1: px, y1: py - 6, x2: px, y2: py + 6 }));
    }
  }

  /* ------------------------------------------------------------------ wiring */
  function num(id) { return parseFloat($(id).value); }
  function manual(robot, action, args) { return api("POST", "/manual", { robot: robot, action: action, args: args }); }

  function wireUp() {
    $("mission-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var text = $("mission-text").value.trim();
      if (!text) { banner("Type a mission first."); return; }
      act($("mission-go"), function () { return api("POST", "/mission", { text: text }); });
    });
    $("change-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var text = $("change-text").value.trim();
      if (!text) return;
      act($("change-go"), function () {
        return api("POST", "/change", { text: text }).then(function (r) { if (r.ok) $("change-text").value = ""; return r; });
      });
    });
    wire("approve", function () { return api("POST", "/approve", { plan_version: S.s.mission.plan_version }); });
    wire("reset", function () { return api("POST", "/reset"); });
    wire("answer-yes", function () { return api("POST", "/change", { text: "yes" }); });
    wire("answer-no", function () { return api("POST", "/change", { text: "no" }); });
    $("mentor").addEventListener("change", function () {
      var el = $("mentor");
      act(el, function () { return api("POST", "/mentor", { present: el.checked }); });
    });
    document.querySelectorAll("[data-m]").forEach(function (b) {
      b.addEventListener("click", function () {
        var m = JSON.parse(b.getAttribute("data-m"));
        act(b, function () { return manual(m.robot, m.action, m.args); });
      });
    });
    wire("d-goto", function () { return manual("drone", "drone_goto", { x: num("d-x"), y: num("d-y"), z: num("d-z") }); });
    wire("d-mark", function () { return manual("drone", "drone_mark", { x: num("d-x"), y: num("d-y") }); });
    wire("r-goto", function () { return manual("rover", "rover_goto", { x: num("r-x"), y: num("r-y") }); });
  }

  wireUp();
  loadArena();
  loadScenarios();
  poll();
  window.fleetpilot = { state: S, render: render };           // handy in the browser console
})();
