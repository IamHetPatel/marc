/* FleetPilot mothership dashboard. Plain JS, no framework, no CDN.
 *
 * Reads GET /state every 500 ms and draws it; every control is one request back to the mothership.
 * The DOM is updated in place (keyed by step id, finding id, message index) rather than rebuilt, so CSS
 * transitions can run: robot markers glide between polls, plan steps animate their state changes, new log
 * rows slide in.
 *
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
  var PADL = 34, PADR = 18, PADT = 16, PADB = 42, SCALE = 140;     // map: metres -> user units, with room for ticks

  var $ = function (id) { return document.getElementById(id); };
  var S = {
    s: null, arena: null, scenarios: [], trail: [], trailMission: null, pick: null,
    busy: new Set(), lost: 0, cache: {}, bannerTimer: null, booted: false,
    steps: new Map(), prevSteps: new Map(), finds: new Map(), logShown: 0, logMission: null,
    lastCurrent: null, userScrollAt: 0, roverAngle: null, markers: {}, seenFirst: {}, section: "mission",
    badgeSeen: { log: null, findings: null }
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
  function icon(name, cls) {
    var s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("class", "ic" + (cls ? " " + cls : ""));
    var u = document.createElementNS(SVGNS, "use");
    u.setAttribute("href", "#i-" + name);
    s.append(u);
    return s;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function changed(key, value) {
    var j = JSON.stringify(value);
    if (S.cache[key] === j) return false;
    S.cache[key] = j;
    return true;
  }
  function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
  function n2(v) { return typeof v === "number" ? v.toFixed(2) : "–"; }
  function hms(t) { return new Date(t * 1000).toTimeString().slice(0, 8); }
  function isIdle() { return !!S.s && IDLE.indexOf(S.s.mission.status) >= 0; }
  function store(key, val) { try { localStorage.setItem(key, val); } catch (e) { /* private window: fine */ } }
  function recall(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

  /* ------------------------------------------------------------------ words people read */
  var ROBOT = { drone: "Scout", rover: "Rover", manager: "Mothership" };
  var LABEL = {
    drone_search: "Search the field", drone_mark: "Mark the spot", drone_land: "Land the scout", drone_home: "Back to the pad", drone_takeoff: "Take off",
    drone_goto: "Fly to a point", drone_scan: "Scan the area", drone_dock: "Dock on the rover",
    rover_goto: "Drive to", rover_search: "Look around", rover_approach: "Close in with the camera",
    rover_inspect: "Inspect it", rover_grip: "Gripper", rover_check_grip: "Check the grip", rover_arm: "Arm",
    rover_pick_up: "Pick it up",
    rover_drive: "Drive", rover_snapshot: "Take a photo", request_authorization: "Ask the operator",
    ask_human: "Ask the operator", report: "Report back", camera_locate: "Find it from above"
  };
  var SOURCE = { ai: "AI plan", fallback: "built-in plan (no AI)", scenario: "scenario", manual: "submitted plan" };

  /* The use cases on the start screen. Scenario files not listed here still show, with their own title. */
  var USECASE = {
    floor_recover_hostile: { title: "Hostile drone in field A", icon: "shield", text: "Mark it, check it with the camera, and carry it to the disposal zone after a person says yes." },
    floor_search_field_a: { title: "Search field A", icon: "target", text: "The scout searches the whole field on its own; the rover goes to what it finds." },
    floor_mark_and_drive: { title: "Mark and drive", icon: "rover", text: "The scout marks the crash site, the rover drives there. The first thing to get working." },
    floor_mark_and_pick: { title: "Mark and pick", icon: "rover", text: "Mark and drive, then the rover grips the object, brings it back to where it started and lets go." },
    t3_recover_hostile: { title: "Hostile drone in field A", icon: "shield", text: "Find it, check it with the camera, and carry it to the disposal zone after a person says yes." },
    own_drone_home: { title: "Our own drone went down", icon: "scout", text: "Find our crashed scout and bring it back to the base. No authorization needed: it is ours." },
    decoy_bottle: { title: "False alarm", icon: "alert", text: "The scout flags something; the rover's camera sees it is just a bottle and leaves it." },
    t4_search_field_a: { title: "Search field A", icon: "target", text: "The scout searches a whole field on its own; the rover goes to whatever it finds." },
    t2_mark_and_drive: { title: "Mark and drive", icon: "rover", text: "The scout marks a crash site, the rover drives there and takes a look." },
    overhead_fetch: { title: "Overhead camera finds it", icon: "camera", text: "A ceiling camera spots the wreck instead of the scout; the rover fetches it." }
  };
  var USECASE_ORDER = ["floor_recover_hostile", "floor_search_field_a", "floor_mark_and_drive", "floor_mark_and_pick", "t3_recover_hostile", "own_drone_home", "decoy_bottle", "t4_search_field_a", "t2_mark_and_drive", "overhead_fetch"];

  /* What a person sees instead of 16 technical steps: the phases of a recovery. */
  var PHASES = [
    { k: "search", label: "Search", text: "The scout sweeps the field from above" },
    { k: "mark", label: "Mark", text: "The scout hovers over what it found" },
    { k: "reach", label: "Reach", text: "The rover drives to the spot" },
    { k: "inspect", label: "Inspect", text: "The rover's camera checks what it is" },
    { k: "authorize", label: "Authorize", text: "A person approves before anything hazardous is touched" },
    { k: "pickup", label: "Pick up", text: "The rover lines up with its camera and grips it" },
    { k: "deliver", label: "Deliver", text: "To the disposal zone, or home if it is ours" },
    { k: "report", label: "Report", text: "Findings and photos for the record" }
  ];
  var PHASE_OF = {
    drone_search: "search", drone_scan: "search", camera_locate: "search", drone_takeoff: "search", drone_goto: "search",
    drone_mark: "mark", drone_home: "reach", drone_land: "reach", drone_dock: "reach", rover_search: "reach",
    rover_drive: "reach", rover_snapshot: "reach", rover_inspect: "inspect", request_authorization: "authorize",
    ask_human: "authorize", rover_approach: "pickup", rover_check_grip: "pickup", rover_pick_up: "pickup", report: "report"
  };
  function phaseKeys(steps) {
    var delivering = false;
    return steps.map(function (st) {
      var t = String((st.args || {}).target || "");
      if (st.action === "rover_goto") {
        if (t === "$home" || t === "$zone.disposal") { delivering = true; return "deliver"; }
        return delivering ? "deliver" : "reach";
      }
      if (st.action === "rover_grip" || st.action === "rover_arm") return delivering ? "deliver" : "pickup";
      return PHASE_OF[st.action] || "reach";
    });
  }
  var POSE_SOURCE = {
    "docked_drone": "Position from the docked scout", "start_cross+odometry": "Dead reckoning from the start cross",
    "fix+odometry": "Last fix plus wheel odometry", "overhead_camera": "Seen by the overhead camera",
    "crazyflie+odometry": "Anchored from a Crazyflie, then wheel odometry", "lighthouse_rider": "Lighthouse, from the Crazyflie it carries",
    "rider+odometry": "Last Lighthouse fix plus wheel odometry"
  };
  function placeName(t) {
    if (t == null) return "";
    t = String(t);
    if (t === "$home") return "the base";
    var m = /^\$finding\.(\w+)$/.exec(t); if (m) return "finding " + m[1];
    m = /^\$zone\.(\w+)$/.exec(t);
    if (m) return m[1] === "disposal" ? "the disposal zone" : m[1].replace(/_/g, " ").replace(/\b(field) (\w)$/, function (_, a, b) { return a + " " + b.toUpperCase(); });
    return t;
  }
  function stepTitle(st) {
    var a = st.args || {}, base = LABEL[st.action] || st.action;
    switch (st.action) {
      case "rover_grip": case "rover_arm": return base + ": " + (a.mode || "");
      case "rover_goto": return base + " " + (a.target ? placeName(a.target) : "(" + a.x + ", " + a.y + ")");
      case "drone_search": case "drone_scan": return a.target ? base.replace("the field", placeName(a.target)).replace("the area", placeName(a.target)) : base;
      case "drone_mark": return base + (a.target ? ": " + placeName(a.target) : "");
      case "rover_search": case "rover_approach": return base + (a.target ? ": " + a.target : "");
      default: return base;
    }
  }
  function summarize(st) {
    var r = st.result;
    if (!r) return st.status === "running" ? { kind: "run", text: "In progress" } : null;
    if (r.ok === false) return { kind: "bad", text: r.error || "failed" };
    var p = [];
    if (r.arrived !== undefined) p.push("arrived, " + r.error_m + " m off");
    if (r.waypoints_flown !== undefined) p.push(r.waypoints_flown + " waypoints flown");
    if (r.findings) p.push(r.findings.length ? r.findings.map(function (f) { return f.id; }).join(", ") + " found" : "nothing found");
    if (r.found !== undefined) p.push(r.found ? "found it" : "not found");
    if (r.in_reach !== undefined) p.push("in reach after " + r.steps + " moves");
    if (r.label) p.push(r.label + (r.category ? ", " + r.category.replace(/_/g, " ") : "") + (r.hazardous ? ", hazardous" : ""));
    if (r.holding !== undefined) p.push(r.holding ? "holding it" : "not holding it");
    if (r.authorized !== undefined) p.push(r.authorized ? "authorized" : "declined");
    if (st.action === "drone_mark" && r.finding_id) p.push("marked " + r.finding_id);
    if (r.answer && st.action === "ask_human") p.push("answer: " + r.answer);
    var warn = r.hazardous || (r.holding === false) || (r.authorized === false);
    return { kind: warn ? "warn" : "ok", text: p.join(", ") || "done" };
  }

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
    setText($("banner-text"), text);
    b.className = "toast" + (kind ? " " + kind : "");
    b.hidden = false;
    requestAnimationFrame(function () { b.classList.add("show"); });
    clearTimeout(S.bannerTimer);
    if (!sticky) S.bannerTimer = setTimeout(hideBanner, 7000);
  }
  function hideBanner() {
    var b = $("banner");
    b.classList.remove("show");
    setTimeout(function () { if (!b.classList.contains("show")) b.hidden = true; }, 260);
  }
  /* Run a request for a button: disabled with a spinner while in flight, the server's 409 / 422 text on failure. */
  function act(el, fn) {
    if (!el) return;
    S.busy.add(el);
    if (el.classList.contains("btn")) el.classList.add("busy");
    render();
    return Promise.resolve().then(fn).then(function (res) {
      if (res && !res.ok) banner(res.error); else hideBanner();
      return res;
    }).then(function (res) { done(el); poll(true); return res; },
            function (e) { done(el); banner(String(e)); });
  }
  function done(el) { S.busy.delete(el); el.classList.remove("busy"); }
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
        if (S.lost >= 2) hideBanner();
        S.lost = 0;
        S.s = r.data;
        updateTrail();
        render();
        if (!S.booted) { S.booted = true; var b = $("boot"); b.classList.add("gone"); setTimeout(function () { b.remove(); }, 700); }
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
      if (r.ok) { S.arena = r.data; manualDefaults(); buildMap(); render(); } else setTimeout(loadArena, 1500);
    });
  }
  /* Start the manual "go to" fields in the middle of the cage and of the arena, whatever frame arena.json uses
     (the real floor's Lighthouse frame has negative coordinates). Only once: after that they are the operator's. */
  function manualDefaults() {
    if (S.cache.manualDefaults) return;
    S.cache.manualDefaults = true;
    var c = S.arena.drone_cage, b = S.arena.bounds;
    $("d-x").value = ((c.x0 + c.x1) / 2).toFixed(2); $("d-y").value = ((c.y0 + c.y1) / 2).toFixed(2);
    $("r-x").value = ((b.x0 + b.x1) / 2).toFixed(2); $("r-y").value = ((b.y0 + b.y1) / 2).toFixed(2);
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
      renderBar(); renderAttention(); renderComposer(); renderPlan(); renderNow(); renderPhases(); renderFleet(); renderTicker();
      renderLive(); renderCamera(); renderOverhead(); renderFindings(); renderLog(); renderManual(); renderScenarios(); renderMap();
    } catch (e) {
      banner("Dashboard error: " + e.message + " (STOP still works)", "", true);
      if (window.console) console.error(e);
    }
  }

  function renderBar() {
    var s = S.s, m = s.mission, st = $("status");
    setText(st, m.status.replace("_", " "));
    st.className = "pill s-" + m.status;
    var src = $("source");
    src.hidden = !m.id;
    setText(src, SOURCE[m.source] || "AI plan");
    setText($("mission-title"), m.text || "");
    [["rover", "rover"], ["drone", "drone"], ["target", "target"]].forEach(function (p) {
      var r = s.robots[p[0]];
      $("light-" + p[1]).className = "light " + (r && r.online ? "on" : "off");
    });
    var mt = s.metrics || {};
    $("light-rover").title = mt.rover_rtt_ms != null ? "Rover link: " + mt.rover_rtt_ms + " ms" : "Rover";
    $("light-drone").title = mt.drone_rtt_ms != null ? "Scout link: " + mt.drone_rtt_ms + " ms" : "Scout";
    var r = s.robots.rover || {}, d = s.robots.drone || {};
    var fleetWarn = !r.online || !d.online || d.pose_ok === false || !!(r.health && r.health.error);
    $("fold-robots").classList.toggle("warn", fleetWarn);
    $("fold-steps").classList.toggle("live", s.mission.status === "running" || s.mission.status === "replanning");
    var mentor = $("mentor");
    if (document.activeElement !== mentor && !S.busy.has(mentor)) mentor.checked = !!s.mentor_present;
    var cal = $("calibration");
    cal.hidden = !(S.arena && !S.arena.calibrated);
    setText(cal, "Uncalibrated geometry");
    cal.title = "arena.json still holds placeholder numbers until they are measured on the floor";
  }

  function renderAttention() {
    var s = S.s, m = s.mission, st = m.status, q = m.pending_question;
    var qc = $("question-card");
    qc.hidden = !q;
    if (q) setText($("question-text"), q);
    setEnabled($("answer-yes"), !!q); setEnabled($("answer-no"), !!q);

    var ap = $("approval-card"), awaiting = st === "awaiting_approval" && !q;
    ap.hidden = !awaiting;
    setEnabled($("approve"), st === "awaiting_approval");
    if (awaiting) setText($("approval-text"), s.plan.length + " steps from the " + (SOURCE[m.source] || "AI plan") +
      ". Nothing moves until you approve." + (m.reason ? " " + m.reason : ""));

    var rc = $("result-card"), terminal = !!m.id && (st === "done" || st === "failed" || st === "stopped");
    rc.hidden = !terminal;
    if (terminal) {
      var ok = st === "done", bad = st === "failed";
      rc.className = "attn res " + (ok ? "ok" : bad ? "bad" : "neutral");
      setText($("result-title"), ok ? "Mission complete" : bad ? "Mission failed" : "Stopped");
      $("result-icon").firstChild.setAttribute("href", ok ? "#i-check" : bad ? "#i-alert" : "#i-stop");
      var f = s.findings.map(function (x) { return x.id + (x.category ? " " + x.category.replace(/_/g, " ") : "") + ", " + x.status; });
      setText($("result-text"), bad ? (m.error || "Something went wrong.") : st === "stopped"
        ? "The rover was halted and the scout told to land. Start over when it is safe." :
        (f.length ? f.join("; ") + "." : (m.reason || "Nothing needed doing.")));
      $("report-link").hidden = false;
    }
    $("report-link-ev").hidden = !m.id;
    var needsYou = !!q || awaiting;                      // something waits for a person: never let it go unnoticed
    $("nav-mission").classList.toggle("alert", needsYou && S.section !== "mission");
  }

  function renderComposer() {
    var st = S.s.mission.status, idle = IDLE.indexOf(st) >= 0;
    var live = LIVE.indexOf(st) >= 0;
    setEnabled($("mission-go"), idle); setEnabled($("mission-text"), idle);
    setEnabled($("change-go"), live); setEnabled($("change-text"), live);
    $("mission-form").hidden = live; $("change-form").hidden = !live;          // one form at a time keeps the plan in view
    setEnabled($("reset"), !!S.s.mission.id && st !== "running" && st !== "replanning");
    document.querySelectorAll("[data-reset]").forEach(function (b) { setEnabled(b, st !== "running" && st !== "replanning"); });
  }

  /* ---- the plan, as a timeline ---- */
  function buildStep(st, delay) {
    var li = h("li", { class: "step pending", style: "animation-delay:" + delay + "ms" });
    li._node = h("span", { class: "node" });
    li._title = h("b", { class: "title" });
    li._act = h("code", { class: "act" });
    li._why = h("div", { class: "why" });
    li._body = h("div", { class: "body" },
      h("div", { class: "head" }, h("span", { class: "who " + st.robot, text: ROBOT[st.robot] || st.robot }), li._title, li._act), li._why);
    li.append(li._node, li._body);
    return li;
  }
  function updateStep(li, st, current) {
    var cls = "step " + st.status + (current ? " current" : "");
    if (li.className !== cls) li.className = cls;
    setText(li._title, stepTitle(st));
    setText(li._act, st.action);
    li.title = st.action;
    setText(li._why, st.why || "");
    var want = st.status === "done" ? "check" : st.status === "failed" ? "x" : st.status === "skipped" ? "minus" : "";
    if (li._icon !== want) { clear(li._node); li._icon = want; if (want) li._node.append(icon(want)); }
    var sum = summarize(st), key = sum ? sum.kind + "|" + sum.text : "";
    if (li._sum !== key) {
      li._sum = key;
      if (li._res) { li._res.remove(); li._res = null; }
      if (sum) {
        li._res = h("div", { class: "res " + sum.kind }, icon(sum.kind === "bad" ? "x" : sum.kind === "warn" ? "alert" : sum.kind === "run" ? "play" : "check"),
          h("span", { text: sum.text }));
        li._body.append(li._res);
      }
    }
  }
  function syncSteps(ol, steps, current, map, prefix) {
    var seen = new Set(), fresh = ol.children.length === 0;
    steps.forEach(function (st, i) {
      var key = prefix + ":" + st.id, li = map.get(key);
      if (!li) { li = buildStep(st, fresh ? Math.min(i, 14) * 28 : 0); map.set(key, li); }
      updateStep(li, st, st.id === current);
      if (ol.children[i] !== li) ol.insertBefore(li, ol.children[i] || null);
      seen.add(key);
    });
    map.forEach(function (li, key) { if (!seen.has(key)) { li.remove(); map.delete(key); } });
  }
  function renderPlan() {
    var s = S.s, m = s.mission, steps = s.plan, doneN = steps.filter(function (x) { return x.status === "done" || x.status === "skipped" || x.status === "failed"; }).length;
    syncSteps($("plan"), steps, s.current_step, S.steps, m.id || "-");
    setText($("plan-count"), doneN + " / " + steps.length);
    var nav = $("plan-nav-count");
    nav.hidden = !steps.length; setText(nav, doneN + "/" + steps.length);
    var bar = $("plan-bar"), pct = steps.length ? Math.round(100 * doneN / steps.length) : 0;
    bar.style.width = pct + "%";
    bar.style.background = m.status === "failed" || m.status === "stopped" ? "var(--crit)" : "var(--good)";
    var empty = $("plan-empty");
    empty.hidden = steps.length > 0 || m.status === "planning";
    renderSkeleton(steps.length === 0 && m.status === "planning");
    var prev = s.previous_plan;
    $("previous").hidden = !prev;
    if (prev) syncSteps($("previous-plan"), prev, null, S.prevSteps, "p" + (m.id || "-") + ":" + m.plan_version);

    // keep the working step in view, unless the person is scrolling around on their own
    if (s.current_step !== S.lastCurrent) { S.lastCurrent = s.current_step; keepCurrentInView(); }
  }
  function keepCurrentInView() {
    var s = S.s, cur = s && s.current_step;
    if (!cur || Date.now() - S.userScrollAt < 4000 || $("fold-steps").classList.contains("folded")) return;
    var li = S.steps.get((s.mission.id || "-") + ":" + cur);
    if (!li) return;
    var box = $("plan-scroll");
    box.scrollTo({ top: Math.max(0, li.offsetTop - box.clientHeight / 2 + li.clientHeight / 2), behavior: "smooth" });
  }
  function renderSkeleton(on) {
    var sk = $("plan-skeleton");
    if (on && !sk) {
      sk = h("div", { id: "plan-skeleton", class: "skeleton", "aria-label": "Planning" });
      for (var i = 0; i < 5; i++) sk.append(h("div", { class: "sk-row" }, h("i"), h("span", { style: "width:" + (45 + (i * 13) % 40) + "%" })));
      $("plan-scroll").prepend(sk);
    } else if (!on && sk) sk.remove();
  }

  /* ---- the "now" card: what is happening, in words ---- */
  function renderNow() {
    var s = S.s, m = s.mission, st = m.status, has = !!m.id;
    $("start-panel").hidden = has;
    $("now-card").hidden = !has;
    if (!has) return;
    var steps = s.plan, cur = null, idx = -1;
    steps.forEach(function (x, i) { if (x.id === s.current_step && (x.status === "running" || st === "running" || st === "replanning")) { cur = x; idx = i; } });
    var keys = phaseKeys(steps), ph = cur ? PHASES.filter(function (p) { return p.k === keys[idx]; })[0] : null;
    var title, why = "";
    if (st === "planning") { title = "Planning the mission"; why = "Turning the mission into steps the robots are allowed to do."; }
    else if (st === "awaiting_approval") { title = "Waiting for your approval"; why = steps.length + " steps are ready. Nothing moves until you approve."; }
    else if (m.pending_question) { title = "Waiting for your decision"; why = "Answer the question above to continue."; }
    else if (cur) { title = stepTitle(cur); why = cur.why || ""; }
    else if (st === "done") { title = "Mission complete"; why = m.reason || ""; }
    else if (st === "failed") { title = "Mission failed"; why = m.error || ""; }
    else if (st === "stopped") { title = "Stopped"; why = "The rover was halted and the scout told to land."; }
    else { title = st.replace("_", " "); }
    setText($("now-phase"), ph ? "Step " + (idx + 1) + " of " + steps.length + " · " + ph.label : "");
    setText($("now-title"), title);
    setText($("now-why"), why);
  }

  /* ---- the phase strip: the recovery story at a glance ---- */
  function renderPhases() {
    var s = S.s, steps = s.plan, keys = phaseKeys(steps), cur = s.current_step, st = s.mission.status;
    var present = PHASES.filter(function (p) { return !steps.length || keys.indexOf(p.k) >= 0; });
    var states = present.map(function (p) {
      if (!steps.length) return "idle";
      var mine = steps.filter(function (x, i) { return keys[i] === p.k; });
      if (mine.some(function (x) { return x.id === cur && (x.status === "running" || st === "running" || st === "replanning"); })) return "active";
      if (mine.every(function (x) { return x.status === "skipped"; })) return "skipped";
      if (mine.every(function (x) { return x.status === "done" || x.status === "skipped"; })) return "done";
      if (mine.some(function (x) { return x.status === "failed"; }) && !mine.some(function (x) { return x.status === "pending"; })) return "failed";
      return "pending";
    });
    if (!changed("phases", [present.map(function (p) { return p.k; }), states])) return;
    var ol = $("phases");
    clear(ol);
    present.forEach(function (p, i) {
      var state = states[i], mark = state === "done" ? icon("check") : state === "failed" ? icon("x") : state === "skipped" ? icon("minus") : h("span", { text: String(i + 1) });
      ol.append(h("li", { class: "phase " + state, title: p.text, "aria-current": state === "active" ? "step" : null },
        h("span", { class: "pnode" }, mark), h("span", { class: "plabel" }, h("b", { text: p.label }), h("small", { text: p.text }))));
    });
  }

  /* ---- robot status strip ---- */
  function renderFleet() {
    var s = S.s, r = s.robots.rover || {}, d = s.robots.drone || {}, t = s.robots.target || {};
    var ru = $("unit-rover"), su = $("unit-scout"), tu = $("unit-target");
    ru.classList.toggle("offline", !r.online); su.classList.toggle("offline", !d.online); tu.classList.toggle("offline", !t.online);
    var rp = r.pose || {};
    setText($("rover-x"), r.online ? n2(rp.x) : "–"); setText($("rover-y"), r.online ? n2(rp.y) : "–");
    setText($("rover-h"), r.online && typeof rp.yaw === "number" ? Math.round(((rp.yaw % 360) + 360) % 360) + "°" : "–");
    setText($("rover-source"), !r.online ? "offline" : (POSE_SOURCE[r.pose_source] || r.pose_source || "no position yet"));
    var rc = $("rover-chip");
    rc.hidden = !r.online; rc.className = "chip" + (r.holding ? " warn" : ""); setText(rc, r.holding ? "holding" : "free");
    var dp = d.pose || {};
    setText($("scout-x"), d.online ? n2(dp.x) : "–"); setText($("scout-y"), d.online ? n2(dp.y) : "–");
    setText($("scout-z"), d.online && typeof dp.z === "number" ? dp.z.toFixed(2) : "–");
    var sc = $("scout-chip");
    sc.hidden = !d.online; sc.className = "chip" + (d.state === "flying" ? " on" : ""); setText(sc, d.state || "");
    var v = d.battery_v, pct = typeof v === "number" ? Math.max(0, Math.min(1, (v - 3.3) / 0.9)) : null, bat = $("battery");
    var segs = bat.children, lit = pct == null ? 0 : Math.ceil(pct * segs.length - 0.001);
    for (var i = 0; i < segs.length; i++) segs[i].classList.toggle("on", i < lit);
    bat.className = "battery" + (pct != null && pct < 0.2 ? " low" : pct != null && pct < 0.45 ? " mid" : "");
    setText($("scout-batt"), pct == null ? "no battery reading" : v.toFixed(2) + " V, " + Math.round(pct * 100) + "%");
    renderHealth(r, d, sc);
    var tp = t.pose || {};
    setText($("target-x"), t.online ? n2(tp.x) : "–"); setText($("target-y"), t.online ? n2(tp.y) : "–");
    setText($("target-note"), t.online ? "Second Crazyflie, read from Lighthouse" : "No target drone detected");
  }

  /* What the robot servers say about themselves (/health, polled by the mothership every 3 s). A lost Lighthouse fix
     is the one that matters most on the floor: the scout's numbers above are then the last good pose, not live. */
  var LIGHTHOUSE = { "estimating position": "Lighthouse OK", "receiving, no position": "Lighthouse: no position yet",
                     "not receiving": "Lighthouse: no base station seen" };
  function renderHealth(r, d, chip) {
    var rh = r.health || {}, dh = d.health || {}, rt = [], el = $("scout-health"), rel = $("rover-health");
    if (rh.error) rt.push(rh.error);                 // the rover server is up but the EP is off or out of range
    else if (r.online) {
      if (typeof rh.battery_pct === "number") rt.push("battery " + rh.battery_pct + "%");
      if (rh.camera) rt.push(rh.camera === "live" ? "camera live" : rh.camera);
    }
    setText(rel, rt.join(" · "));
    rel.className = rh.error ? "bad" : "muted";
    if (d.online && d.pose_ok === false) {
      chip.hidden = false; chip.className = "chip warn"; setText(chip, "no fix");
      setText(el, d.pose_error || "position not trusted"); el.className = "bad";
      return;
    }
    var bs = dh.base_stations, st = dh.lighthouse ? (LIGHTHOUSE[dh.lighthouse] || "Lighthouse: " + dh.lighthouse) : "";
    if (st && bs && bs.length) st += " (" + bs.length + " base station" + (bs.length > 1 ? "s" : "") + ")";
    setText(el, !d.online || dh.mock ? "" : dh.hint || st);
    el.className = dh.hint ? "bad" : "muted";
  }

  function renderTicker() {
    var msgs = S.s.messages, el = $("ticker-text");
    if (!msgs.length) { setText(el, "Waiting for a mission."); setText($("ticker-role"), "AI"); $("ticker-role").className = "role"; return; }
    var m = msgs[msgs.length - 1];
    if (el.textContent !== m.text) {
      setText(el, m.text);
      el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
    }
    setText($("ticker-role"), m.role === "human" ? "You" : m.role === "ai" ? "AI" : "System");
    $("ticker-role").className = "role " + m.role;
  }

  function renderCamera() {
    var id = S.s.last_image_id, img = $("camera");
    $("camera-empty").hidden = !!id;
    img.hidden = !id;
    if (id && S.cache.cam !== id) { S.cache.cam = id; img.src = "/images/" + encodeURIComponent(id) + ".jpg"; }
  }

  /* The real rover's live MJPEG, relayed by the mothership (/rover/video). One long-lived request while the card is
     shown; a dropped stream is retried after a pause, never in a tight loop. The photo card below keeps showing the
     latest stored image (the evidence the AI looked at). */
  var LIVE_RETRY_MS = 3000;
  function renderLive() {
    var r = S.s.robots.rover || {}, on = !!(r.online && r.health && r.health.video), img = $("live");
    $("live-card").hidden = !on;
    setText($("camera-label"), on ? "Last photo" : "Rover camera");
    setText($("live-info"), on && r.health.battery_pct != null ? r.health.battery_pct + "%" : "");
    if (!on) {
      if (S.cache.live) { S.cache.live = false; img.removeAttribute("src"); img.hidden = true; $("live-empty").hidden = false; }
      return;
    }
    if (S.cache.live || Date.now() < (S.cache.liveRetry || 0)) return;
    S.cache.live = true;
    img.onerror = function () {
      S.cache.live = false; S.cache.liveRetry = Date.now() + LIVE_RETRY_MS;
      img.hidden = true; $("live-empty").hidden = false;
    };
    img.hidden = false; $("live-empty").hidden = true;
    img.src = "/rover/video?t=" + Date.now();
  }

  /* The overhead webcam frame is not a stored image: it is fetched live, at most once a second, only while the camera
     is installed and a frame has loaded (no request pile-up on a slow link). */
  var OVERHEAD_PERIOD_MS = 1000;
  function renderOverhead() {
    var o = S.s.robots.overhead, card = $("overhead-card");
    card.hidden = !o;
    if (!o) return;
    var tags = o.floor_tags_seen + "/" + o.floor_tags_total + " tags";
    setText($("overhead-info"), !o.online ? "offline" : !o.calibrated ? tags + " · uncalibrated"
      : tags + (o.rover_seen ? " · rover" : " · no rover"));
    var img = $("overhead"), now = Date.now();
    if (!o.online || S.cache.ovBusy || now - (S.cache.ovT || 0) < OVERHEAD_PERIOD_MS) return;
    S.cache.ovBusy = true; S.cache.ovT = now;
    var next = new Image();
    next.onload = function () { img.src = next.src; img.hidden = false; $("overhead-empty").hidden = true; S.cache.ovBusy = false; };
    next.onerror = function () { S.cache.ovBusy = false; };
    next.src = "/overhead.jpg?t=" + now;
  }

  /* ---- findings table ---- */
  var STATUS = {
    found: { cls: "", ic: "info", label: "found" }, inspected: { cls: "warn", ic: "target", label: "inspected" },
    recovered: { cls: "good", ic: "check", label: "recovered" }, skipped: { cls: "", ic: "minus", label: "skipped" }
  };
  function badge(kind, ic, label) { return h("span", { class: "status-badge " + kind }, icon(ic), label); }
  function findingCells(f) {
    var st = STATUS[f.status] || STATUS.found;
    return [
      h("td", { class: "photo" }, f.image_id ? h("img", { src: "/images/" + encodeURIComponent(f.image_id) + ".jpg", alt: "evidence for " + f.id, "data-zoom": "1" }) : h("span", { class: "noimg" }, icon("camera"))),
      h("td", null, h("b", { class: "what", text: f.category ? f.category.replace(/_/g, " ") : (f.label || "unidentified") }), h("small", { class: "fid", text: f.id })),
      h("td", null, f.hazardous == null ? "—" : f.hazardous ? badge("bad", "alert", "hazardous") : badge("good", "check", "safe")),
      h("td", { text: f.authorized ? "yes" : "—" }),
      h("td", null, badge(st.cls, st.ic, st.label)),
      h("td", { class: "m", text: n2(f.x) + ", " + n2(f.y) })
    ];
  }
  function renderFindings() {
    var list = S.s.findings, tb = document.querySelector("#findings tbody");
    $("findings-empty").hidden = list.length > 0;
    setText($("findings-count"), String(list.length));
    bump("findings", list.length);
    var seen = new Set();
    list.forEach(function (f, i) {
      var tr = S.finds.get(f.id), sig = JSON.stringify(f);
      if (!tr) { tr = h("tr"); S.finds.set(f.id, tr); }
      if (tr._sig !== sig) { tr._sig = sig; clear(tr); findingCells(f).forEach(function (c) { tr.append(c); }); }
      if (tb.children[i] !== tr) tb.insertBefore(tr, tb.children[i] || null);
      seen.add(f.id);
    });
    S.finds.forEach(function (tr, id) { if (!seen.has(id)) { tr.remove(); S.finds.delete(id); } });
  }

  /* ---- log ---- */
  function renderLog() {
    var msgs = S.s.messages, ul = $("log"), id = S.s.mission.id;
    if (S.logMission !== id || msgs.length < S.logShown) { clear(ul); S.logShown = 0; S.logMission = id; S.badgeSeen.log = null; }
    $("log-empty").hidden = msgs.length > 0;
    setText($("log-count"), String(msgs.length));
    if (msgs.length === S.logShown) return;
    var atBottom = ul.parentNode.scrollHeight - ul.parentNode.scrollTop - ul.parentNode.clientHeight < 50;
    for (var i = S.logShown; i < msgs.length; i++) {
      var m = msgs[i];
      ul.append(h("li", { class: m.role },
        h("time", { text: hms(m.t) }),
        h("span", { class: "role " + m.role, text: m.role === "human" ? "You" : m.role === "ai" ? "AI" : "System" }),
        h("span", { class: "text", text: m.text })));
    }
    S.logShown = msgs.length;
    bump("log", msgs.length);
    if (atBottom) ul.parentNode.scrollTop = ul.parentNode.scrollHeight;
  }
  function bump(tab, n) {                 // a quiet nudge on a tab whose content grew while you were looking elsewhere
    var prev = S.badgeSeen[tab];
    if (prev != null && n > prev && S.section !== "evidence") {
      var el = $(tab + "-count");
      el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump");
    }
    S.badgeSeen[tab] = n;
  }

  /* ---- manual control + scenarios ---- */
  function renderManual() {
    var idle = isIdle();
    document.querySelectorAll("#fold-manual .fold-body button").forEach(function (b) { setEnabled(b, idle); });
    setText($("manual-hint"), idle ? "Drive the robots by hand between missions. Every action still passes through the safety checks."
      : "Manual control is for between missions. Use Stop to end the current one.");
    var m = S.s.manual, box = $("manual-last");
    if (!m) { setText(box, ""); box.className = "manual-last"; return; }
    var args = Object.keys(m.args || {}).map(function (k) { return k + "=" + m.args[k]; }).join(" ");
    var res = m.status === "running" ? "running..." : m.status === "done" ? "done" : "failed: " + ((m.result && m.result.error) || "");
    setText(box, "last: " + m.action + (args ? " " + args : "") + " → " + res);
    box.className = "manual-last " + (m.status === "failed" ? "bad" : m.status === "done" ? "ok" : "");
  }
  function usecaseCard(sc, delay) {
    var u = USECASE[sc.name] || { title: sc.title, icon: "play", text: sc.has_plan ? "A ready-made plan." : "The AI plans it from the mission." };
    var b = h("button", { type: "button", class: "usecase", "data-scenario": sc.name, title: sc.title },
      h("span", { class: "uc-ic" }, icon(u.icon)),
      h("span", { class: "uc-t" }, h("b", { text: u.title }), h("small", { text: u.text })),
      h("span", { class: "uc-go" }, icon("play")));
    b.addEventListener("click", function () {
      act(b, function () { return api("POST", "/scenarios/" + encodeURIComponent(sc.name) + "/load"); });
    });
    return h("li", { style: "animation-delay:" + delay + "ms" }, b);
  }
  function renderScenarios() {
    if (changed("scenarios", S.scenarios)) {
      var ul = $("start-list"), rank = function (sc) { var i = USECASE_ORDER.indexOf(sc.name); return i < 0 ? 99 : i; };
      clear(ul);
      S.scenarios.slice().sort(function (a, b) { return rank(a) - rank(b); })
        .forEach(function (sc, i) { ul.append(usecaseCard(sc, i * 40)); });
    }
    document.querySelectorAll("[data-scenario]").forEach(function (b) { setEnabled(b, isIdle()); });
  }

  /* ------------------------------------------------------------------ map */
  /* The map shows the arena's `view` (the whole net plus the launch pad, from /arena), grown to include any robot or
     pad that turns up outside it: a scout taking off from outside the arena must stay on the map. */
  var VIEW_MARGIN = 0.25;
  function viewRect() {
    var v = S.arena.view || S.arena.bounds, r = { x0: v.x0, y0: v.y0, x1: v.x1, y1: v.y1 };
    (S.viewExtra || []).forEach(function (p) {
      r.x0 = Math.min(r.x0, p[0] - VIEW_MARGIN); r.x1 = Math.max(r.x1, p[0] + VIEW_MARGIN);
      r.y0 = Math.min(r.y0, p[1] - VIEW_MARGIN); r.y1 = Math.max(r.y1, p[1] + VIEW_MARGIN);
    });
    return r;
  }
  function inView(x, y) { var v = S.view; return x >= v.x0 && x <= v.x1 && y >= v.y0 && y <= v.y1; }
  function geo() {
    var b = S.view;
    return { b: b, w: (b.x1 - b.x0) * SCALE + PADL + PADR, h: (b.y1 - b.y0) * SCALE + PADT + PADB };
  }
  function sx(x) { return PADL + (x - S.view.x0) * SCALE; }
  function sy(y) { return PADT + (S.view.y1 - y) * SCALE; }
  function mx(px) { return (px - PADL) / SCALE + S.view.x0; }
  function my(py) { return S.view.y1 - (py - PADT) / SCALE; }
  function padPoint() {
    var d = S.s && S.s.robots.drone, h = d && d.home;
    if (h && typeof h.x === "number") return h;
    return S.arena.launch_pad || null;
  }

  function buildMap() {
    S.view = viewRect();
    S.seenFirst = {};
    ["search", "trail", "pad"].forEach(function (k) { delete S.cache[k]; });     // redraw these on the new scale
    var g = geo(), svg = $("map"), a = S.arena, b = g.b, ar = a.bounds;
    clear(svg);
    svg.setAttribute("viewBox", "0 0 " + g.w + " " + g.h);
    var step = 0.5 * SCALE;
    svg.append(sv("defs", null,
      sv("pattern", { id: "hatch", width: 9, height: 9, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, sv("line", { class: "hatch-line", x1: 0, y1: 0, x2: 0, y2: 9 })),
      sv("pattern", { id: "dots", width: step, height: step, patternUnits: "userSpaceOnUse", x: PADL, y: PADT }, sv("circle", { class: "dots", cx: 0, cy: 0, r: 1.1 }))));
    var st = sv("g", { id: "map-static" });
    st.append(sv("rect", { x: sx(b.x0), y: sy(b.y1), width: (b.x1 - b.x0) * SCALE, height: (b.y1 - b.y0) * SCALE, fill: "url(#dots)" }));
    Object.keys(a.zones).forEach(function (name) {
      var z = a.zones[name];
      if (name === "test_zone") return;                          // identical to the cage
      st.append(sv("rect", { class: "zone " + name, x: sx(z.x0), y: sy(z.y1), width: (z.x1 - z.x0) * SCALE, height: (z.y1 - z.y0) * SCALE }));
      st.append(sv("text", { class: "zlabel", x: sx(z.x0) + 8, y: sy(z.y1) + 16, text: name.replace(/_/g, " ") }));
    });
    var c = a.drone_cage, cx0 = sx(c.x0), cy0 = sy(c.y1), cw = (c.x1 - c.x0) * SCALE, ch = (c.y1 - c.y0) * SCALE;
    st.append(sv("rect", { class: "cage", x: cx0, y: cy0, width: cw, height: ch }));
    [[cx0, cy0, 1, 1], [cx0 + cw, cy0, -1, 1], [cx0, cy0 + ch, 1, -1], [cx0 + cw, cy0 + ch, -1, -1]].forEach(function (k) {
      st.append(sv("path", { class: "cage-tick", d: "M" + (k[0] + k[2] * 12) + " " + k[1] + "H" + k[0] + "V" + (k[1] + k[3] * 12), fill: "none" }));
    });
    st.append(sv("text", { class: "tick", x: cx0 + cw - 2, y: cy0 - 6, "text-anchor": "end", text: "FLIGHT CAGE" }));
    st.append(sv("rect", { class: "bounds", x: sx(ar.x0), y: sy(ar.y1), width: (ar.x1 - ar.x0) * SCALE, height: (ar.y1 - ar.y0) * SCALE, fill: "none" }));
    st.append(sv("text", { class: "tick", x: sx(ar.x0) + 2, y: sy(ar.y1) - 6, text: "ARENA" }));
    for (var x = Math.ceil(b.x0); x <= b.x1; x++) st.append(sv("text", { class: "tick", x: sx(x), y: sy(b.y0) + 17, "text-anchor": "middle", text: x + " m" }));
    for (var y = Math.ceil(b.y0); y <= b.y1; y++) st.append(sv("text", { class: "tick", x: PADL - 8, y: sy(y) + 3.5, "text-anchor": "end", text: y + "" }));
    var hm = a.home;
    st.append(sv("path", { class: "home", d: "M" + (sx(hm.x) - 6) + " " + sy(hm.y) + "h12M" + sx(hm.x) + " " + (sy(hm.y) - 6) + "v12" }));
    var barY = g.h - 9, barX1 = g.w - PADR, barX0 = barX1 - SCALE;
    st.append(sv("line", { class: "scalebar", x1: barX0, y1: barY, x2: barX1, y2: barY }));
    st.append(sv("path", { class: "scalebar", d: "M" + barX0 + " " + (barY - 4) + "v8M" + barX1 + " " + (barY - 4) + "v8", fill: "none" }));
    st.append(sv("text", { class: "scalelabel", x: barX0 - 8, y: barY + 3.5, "text-anchor": "end", text: "1 m" }));

    var dyn = sv("g", { id: "map-dynamic" });
    var pad = sv("g", { class: "mk", id: "mk-pad" }, sv("circle", { class: "pad-ring", r: 13 }), sv("path", { class: "pad-h", d: "M-4 -5V5M4 -5V5M-4 0H4" }),
      sv("text", { class: "tag-text dim", x: 0, y: 28, "text-anchor": "middle", text: "launch pad" }));
    S.markers = {
      path: sv("polyline", { class: "path" }), waypoints: sv("g"), trail: sv("polyline", { class: "trail" }), finds: sv("g"),
      pick: sv("g", { class: "pick" })
    };
    var tgt = sv("g", { class: "mk", id: "mk-target" }, sv("circle", { class: "beacon", r: 12 }), sv("circle", { class: "target-ring", r: 12 }),
      sv("path", { class: "target-x", d: "M-4 -4L4 4M4 -4L-4 4" }), sv("text", { class: "tag-text dim", x: 0, y: 28, "text-anchor": "middle", text: "truth" }));
    var rotors = [[-8, -8], [8, -8], [-8, 8], [8, 8]].map(function (p) { return sv("circle", { class: "scout-rotor", cx: p[0], cy: p[1], r: 5.5 }); });
    var scoutLabel = sv("text", { class: "tag-text", x: 0, y: 30, "text-anchor": "middle", text: "scout" });
    var scout = sv("g", { class: "mk scout landed", id: "mk-scout" }, sv("circle", { class: "scout-halo", r: 16 }),
      sv("path", { class: "scout-arm", d: "M-8 -8L8 8M8 -8L-8 8" }), rotors[0], rotors[1], rotors[2], rotors[3], sv("circle", { class: "scout-core", r: 4.2 }), scoutLabel);
    var rot = sv("g", { class: "rot" }, sv("line", { class: "rover-ray", x1: 14, y1: 0, x2: 40, y2: 0 }),
      sv("path", { class: "rover-body", d: "M15 0L4 -10L-12 -10L-12 10L4 10Z" }), sv("circle", { class: "rover-nose", cx: 7, cy: 0, r: 2.2 }));
    var roverLabel = sv("text", { class: "tag-text", x: 0, y: -18, "text-anchor": "middle", text: "rover" });
    var rover = sv("g", { class: "mk", id: "mk-rover" }, rot, roverLabel);
    S.markers.target = tgt; S.markers.scout = scout; S.markers.scoutLabel = scoutLabel; S.markers.rover = rover; S.markers.rot = rot;
    S.markers.halo = scout.querySelector(".scout-halo");
    var hair = sv("g", { id: "crosshair", style: "display:none" }, sv("line", { class: "xh", id: "xh-v", y1: sy(b.y1), y2: sy(b.y0) }), sv("line", { class: "xh", id: "xh-h", x1: sx(b.x0), x2: sx(b.x1) }));
    S.markers.pad = pad;
    dyn.append(pad, S.markers.path, S.markers.waypoints, S.markers.trail, tgt, S.markers.finds, scout, rover, S.markers.pick, hair);
    [pad, tgt, scout, rover].forEach(function (m) { m.style.display = "none"; });
    svg.append(st, dyn);
    if (S.mapWired) return;                                        // a rebuild (view grew) keeps the listeners
    S.mapWired = true;

    var frame = 0;
    svg.addEventListener("mousemove", function (ev) {
      if (frame) return;
      frame = requestAnimationFrame(function () {
        frame = 0;
        var p = clientToSvg(ev);
        var inside = inView(mx(p.x), my(p.y));
        $("crosshair").style.display = inside ? "" : "none";
        if (inside) {
          $("xh-v").setAttribute("x1", p.x); $("xh-v").setAttribute("x2", p.x); $("xh-h").setAttribute("y1", p.y); $("xh-h").setAttribute("y2", p.y);
          setText($("readout"), "x " + mx(p.x).toFixed(2) + " · y " + my(p.y).toFixed(2) + " m");
        }
      });
    });
    svg.addEventListener("mouseleave", function () { $("crosshair").style.display = "none"; setText($("readout"), S.pick ? pickText() : "x – · y –"); });
    svg.addEventListener("click", function (ev) {
      var p = clientToSvg(ev), x = mx(p.x), y = my(p.y);
      if (!inView(x, y)) return;
      S.pick = { x: x, y: y };
      ["d-x", "r-x"].forEach(function (id) { $(id).value = x.toFixed(2); });
      ["d-y", "r-y"].forEach(function (id) { $(id).value = y.toFixed(2); });
      if ($("fold-manual").classList.contains("folded")) setFold("manual", true, true);
      renderMap();
    });
  }
  function pickText() { return "picked x " + S.pick.x.toFixed(2) + " · y " + S.pick.y.toFixed(2) + " m"; }
  function clientToSvg(ev) {
    var svg = $("map"), pt = svg.createSVGPoint();
    pt.x = ev.clientX; pt.y = ev.clientY;
    return pt.matrixTransform(svg.getScreenCTM().inverse());
  }
  /* Place a marker. The first time it is shown it snaps into place; after that CSS glides it between polls. */
  function place(key, el, x, y) {
    var first = !S.seenFirst[key];
    el.style.display = "";
    if (first) el.style.transition = "none";
    el.style.transform = "translate(" + sx(x) + "px," + sy(y) + "px)";
    if (first) { S.seenFirst[key] = true; void el.getBoundingClientRect(); el.style.transition = ""; }
  }
  function renderMap() {
    if (!S.arena || !S.s || !S.markers.rover) return;
    var s = S.s, M = S.markers;
    var r = s.robots.rover, d = s.robots.drone, t = s.robots.target;
    var outside = [r && r.pose, d && d.pose, t && t.pose, padPoint()].filter(function (p) {
      return p && typeof p.x === "number" && !inView(p.x, p.y);
    });
    if (outside.length) {
      S.viewExtra = (S.viewExtra || []).concat(outside.map(function (p) { return [p.x, p.y]; }));
      buildMap(); M = S.markers;
    }
    var pad = padPoint();
    if (pad) place("pad", M.pad, pad.x, pad.y); else M.pad.style.display = "none";

    // the planned search pattern, from the latest drone_search result
    var search = null;
    s.plan.forEach(function (st) { if (st.action === "drone_search" && st.result && st.result.search_path) search = st.result.search_path; });
    if (changed("search", search)) {
      clear(M.waypoints);
      M.path.setAttribute("points", search ? search.map(function (p) { return sx(p[0]) + "," + sy(p[1]); }).join(" ") : "");
      if (search) search.forEach(function (p) { M.waypoints.append(sv("circle", { class: "waypoint", cx: sx(p[0]), cy: sy(p[1]), r: 2.2 })); });
    }
    if (changed("trail", [S.trail.length, S.trail[S.trail.length - 1]])) M.trail.setAttribute("points", S.trail.map(function (p) { return sx(p[0]) + "," + sy(p[1]); }).join(" "));

    if (t && t.online && t.pose && typeof t.pose.x === "number") place("target", M.target, t.pose.x, t.pose.y); else M.target.style.display = "none";

    s.findings.forEach(function (f) {
      var outer = S.markers["f_" + f.id];
      if (!outer) {
        outer = sv("g", { transform: "translate(" + sx(f.x) + "," + sy(f.y) + ")" },
          sv("g", { class: "finding" }, sv("path", { class: "finding-line", d: "M-6 -6L6 6M6 -6L-6 6" }), sv("text", { class: "tag-text", x: 11, y: -9 })));
        M.finds.append(outer); S.markers["f_" + f.id] = outer;
      }
      var inner = outer.firstChild;
      inner.setAttribute("class", "finding" + (f.status === "recovered" ? " recovered" : f.status === "skipped" ? " skipped" : f.hazardous ? " hazardous" : ""));
      setText(inner.lastChild, f.id + (f.category ? " " + f.category.replace(/_/g, " ") : ""));
    });
    Object.keys(M).forEach(function (k) {
      if (k.indexOf("f_") === 0 && !s.findings.some(function (f) { return "f_" + f.id === k; })) { M[k].remove(); delete M[k]; }
    });

    if (d && d.online && d.pose && typeof d.pose.x === "number") {
      place("scout", M.scout, d.pose.x, d.pose.y);
      var flying = d.state === "flying";
      M.scout.setAttribute("class", "mk scout " + (flying ? "flying" : "landed"));
      M.halo.setAttribute("r", flying ? 14 + 16 * (d.pose.z || 0) : 14);
      var onPad = !flying && pad && Math.hypot(d.pose.x - pad.x, d.pose.y - pad.y) < 0.15;
      setText(M.scoutLabel, flying ? "scout · z " + n2(d.pose.z) : onPad || d.state === "docked" ? "scout · on pad" : "scout · landed");
      M.pad.lastChild.style.display = onPad ? "none" : "";          // the scout's label already says "on pad"
    } else M.scout.style.display = "none";

    if (r && r.online && r.pose && typeof r.pose.x === "number") {
      place("rover", M.rover, r.pose.x, r.pose.y);
      var want = -(r.pose.yaw || 0);                              // SVG y points down, yaw is counter-clockwise
      if (S.roverAngle == null) S.roverAngle = want;
      else S.roverAngle += ((want - S.roverAngle + 540) % 360) - 180;   // shortest way round, never a full spin
      M.rot.style.transform = "rotate(" + S.roverAngle + "deg)";
    } else M.rover.style.display = "none";

    M.pick.replaceChildren();
    if (S.pick) {
      var px = sx(S.pick.x), py = sy(S.pick.y);
      M.pick.append(sv("path", { d: "M" + (px - 7) + " " + py + "h14M" + px + " " + (py - 7) + "v14", fill: "none" }),
        sv("circle", { cx: px, cy: py, r: 9, fill: "none" }));
      if ($("readout").textContent.indexOf("x –") === 0) setText($("readout"), pickText());
    }
  }

  /* ------------------------------------------------------------------ sections, theme, lightbox */
  var VIEWS = ["mission", "evidence"];
  function selectView(name) {
    if (VIEWS.indexOf(name) < 0) name = "mission";
    S.section = name;
    document.querySelectorAll(".vtab").forEach(function (t) { t.setAttribute("aria-selected", String(t.getAttribute("data-view") === name)); });
    VIEWS.forEach(function (v) { $("view-" + v).hidden = v !== name; });
    if (name === "evidence") { var el = $("findings-count"); el.classList.remove("bump"); }
    if (name === "mission") $("nav-mission").classList.remove("alert");
    store("fp-view", name);
  }
  /* Panels beside the map fold open or closed; the choice is remembered per panel. */
  function setFold(name, open, scroll) {
    var sec = $("fold-" + name);
    if (!sec) return;
    sec.classList.toggle("folded", !open);
    sec.querySelector(".fold-head").setAttribute("aria-expanded", String(open));
    store("fp-fold-" + name, open ? "open" : "closed");
    if (open && name === "steps") { S.userScrollAt = 0; keepCurrentInView(); }
    if (open && scroll) sec.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function openLightbox(src) {
    if (!src) return;
    $("lightbox-img").src = src;
    $("lightbox").hidden = false;
  }

  /* ------------------------------------------------------------------ wiring */
  function num(id) { return parseFloat($(id).value); }
  function manual(robot, action, args) { return api("POST", "/manual", { robot: robot, action: action, args: args }); }
  function reset() { return api("POST", "/reset"); }

  function wireUp() {
    $("mission-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var text = $("mission-text").value.trim();
      if (!text) { banner("Type a mission first.", "info"); return; }
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
    wire("reset", reset);
    document.querySelectorAll("[data-reset]").forEach(function (b) { b.addEventListener("click", function () { act(b, reset); }); });
    $("adjust").addEventListener("click", function () { selectView("mission"); $("change-text").focus(); });
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
    wire("r-home", function () { return api("POST", "/rover/home"); });
    wire("r-anchor-target", function () { return api("POST", "/rover/anchor", { cf: "target" }); });
    wire("r-anchor-scout", function () { return api("POST", "/rover/anchor", { cf: "scout" }); });

    document.querySelectorAll(".vtab").forEach(function (t) {
      t.addEventListener("click", function () { selectView(t.getAttribute("data-view")); });
    });
    $("see-plan").addEventListener("click", function () { setFold("steps", true, true); });
    document.querySelectorAll("[data-fold]").forEach(function (sec) {
      var name = sec.getAttribute("data-fold"), saved = recall("fp-fold-" + name);
      if (saved) setFold(name, saved === "open");
      sec.querySelector(".fold-head").addEventListener("click", function () { setFold(name, sec.classList.contains("folded")); });
    });
    $("to-evidence").addEventListener("click", function () { selectView("evidence"); });
    $("theme").addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      store("fp-theme", next);
    });
    $("camera-card").addEventListener("click", function () { if (S.s && S.s.last_image_id) openLightbox($("camera").src); });
    $("overhead-card").addEventListener("click", function () { if (!$("overhead").hidden) openLightbox($("overhead").src); });
    $("live-card").addEventListener("click", function () { if (!$("live").hidden) openLightbox("/rover/video?t=" + Date.now()); });
    $("lightbox").addEventListener("click", function () { $("lightbox").hidden = true; });
    document.addEventListener("click", function (ev) { if (ev.target.getAttribute && ev.target.getAttribute("data-zoom")) openLightbox(ev.target.src); });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && !$("lightbox").hidden) $("lightbox").hidden = true;
      var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((ev.target.tagName || ""));
      if (ev.key === "/" && !typing && !$("mission-text").disabled) { ev.preventDefault(); selectView("mission"); $("mission-text").focus(); }
    });
    var box = $("plan-scroll");
    // only real scrolling counts as "the person is looking around": wheel, a touch drag, or grabbing the scrollbar
    // itself (a press that lands on the panel's own edge). Clicking a button inside it does not.
    ["wheel", "touchmove"].forEach(function (n) { box.addEventListener(n, function () { S.userScrollAt = Date.now(); }, { passive: true }); });
    box.addEventListener("pointerdown", function (ev) { if (ev.target === box) S.userScrollAt = Date.now(); }, { passive: true });
    // a question or approval card appearing above the plan shrinks it: keep the working step in view
    if (window.ResizeObserver) new ResizeObserver(function () { keepCurrentInView(); }).observe(box);

    selectView(recall("fp-view") || "mission");
  }

  wireUp();
  loadArena();
  loadScenarios();
  poll();
  window.fleetpilot = { state: S, render: render };           // handy in the browser console
})();
