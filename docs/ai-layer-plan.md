# AI layer plan: make the existing AI manager live, guarded and budget-aware

**For:** agents (and teammates) implementing the next AI step on `feat/ai-manager`. **Read first:**
[integration-plan.md](integration-plan.md) (how the pieces run) and [ai-manager-plan.md §17](ai-manager-plan.md)
(threading and epoch rules in `controller.py` / `executor.py`). Where this file disagrees with the older plans about
the AI layer, this file wins.

**Rules for every work package (WP):** one commit per WP; `pytest` (mocks, ~40 s) must pass before and after;
never push; never print or log the API key; nothing in this plan may make a demo depend on the LLM (no key =
built-in plans + rules, exactly as today).

---

## 0. Where we are (verified 2026-10-11)

| Check | Result |
| --- | --- |
| Key in `.env` (`GEMINI_API_KEY`) | **Works** (exported in the shell). `.env` is git-ignored, but **nothing loads it**: it only works because the shell exported it |
| `pytest -m live` | 1 passed (planner), 1 failed: `test_structured_output_with_an_image_in_one_call` expects the **old** inspect fields; the model's answer was correct and now also carries `category`, `hazardous`, `reason` (M6). Test is stale, not the code |
| `gemini-robotics-er-2-preview` | Works (4.2 s for an image question), but the **free tier allows 20 requests per day for this model**; it is used up (`429 RESOURCE_EXHAUSTED`, retry in ~1.5 h) |
| Fallbacks in `Config.model_fallbacks` | **Both dead for this key:** `gemini-robotics-er-1.5-preview` → 404; `gemini-2.5-flash` → 404 "no longer available to new users" |
| Models probed with one structured-output call each | `gemini-3.8-flash` OK 2.6 s · `gemini-flash-latest` OK 2.3 s · `gemini-3.5-flash-lite` OK 0.9 s |
| Result in your run | Every supervisor call fell through all three models and ended in the **rule supervisor** (the mission kept going, as designed) |

---

## 1. The AI layer as built (map for the implementer)

```
mission text ─► planner.py (LLM: tag "plan") ─► validate_drafts ─► human Approve ─► executor.py ─► tools.run_action
                                                                                        │
                       supervisor.py (LLM: tag "supervisor") ◄── checkpoints / failures / typed changes
                       perception.py (LLM: tags "point", "inspect", "holding")  ◄── rover_search / rover_approach / rover_inspect / rover_check_grip
                       overhead.py  (LLM: tag "overhead_point")                  ◄── camera_locate (overhead webcam)
llm.py  GeminiLLM.generate(prompt, schema, images, tag)   # the ONLY Gemini caller; logs every call to runs/<ts>/
```

| File | Role | Key functions |
| --- | --- | --- |
| `manager/llm.py` | One Gemini wrapper: structured JSON output, fallback chain (`[MODEL, *model_fallbacks]`, sticky on the first that answers), call log | `GeminiLLM.generate`, `ScriptedLLM` (tests), `make_llm` (None without a key) |
| `manager/planner.py` | Text → plan. Rules text + arena + robot status + the action vocabulary (generated from `tools.py` docstrings) → `PlanDraft`; invalid output re-requested once, then the canned plan | `Planner.plan`, `validate_drafts`, `rules_text`, `arena_text` |
| `manager/supervisor.py` | Decides at checkpoints, failures and typed changes: `continue / replan / done / abort / ask_human`; LLM output validated; falls back to `RuleSupervisor` | `LLMSupervisor.decide`, `RuleSupervisor.decide` |
| `manager/perception.py` | Camera questions: point at a target, inspect (triage), is it in the gripper | `GeminiPerception`, `MockPerception` (ground truth, used whenever the rover is mocked) |
| `manager/tools.py` | The action vocabulary and the cross-cutting guards (`run_action`) | `REGISTRY`, `validate_step`, `run_action`, `_authorization_gate`, `_separation_violation` |
| `manager/controller.py` | State machine, approvals, changes, manual control, anchoring, report | `start_mission`, `submit_plan`, `load_scenario`, `approve`, `change`, `stop`, `manual` |
| `manager/scenarios.py` | YAML scenarios (plan and/or mission + targets); id = file name, slug-checked | `load_scenario`, `list_scenarios` |
| `manager/server.py` | HTTP API on :8002 + dashboard | see its docstring |
| `manager/static/` | Dashboard: mission box, change box, approval card, question card, plan list, map, manual control | `renderComposer`, `renderPlan`, `renderAttention` in `app.js` |

### 1.1 Guard rails that already exist (do not weaken any of them)

1. **The AI only ever writes plans** (`PlanDraft` / `SupervisorDecision`); it has no tool to change code, config, the
   arena, safety values or files.
2. **Fixed action vocabulary:** a step with an unknown action, the wrong robot or missing arguments is rejected
   (`validate_step`, `validate_drafts`, max 30 steps). Planner: rejected twice → canned plan. Supervisor: rejected
   twice → rule supervisor.
3. **Human approval** before anything runs; `/approve` names the plan version (`409` if it changed).
4. **Safety in code, applied to every plan source** (`tools.run_action`, `safety.py`): geofences, speed/move caps,
   mentor gate for flights, authorization gate before gripping a hazardous find, rover/drone separation, out-of-area
   stop, replan budget (2 per step, 6 per mission), mission timeout.
5. **Stop never touches the LLM** and works from every state; the watchdog stops robots that stop answering.
6. **No key / dead model → rules**, never a crash (`make_llm` returns None; `LLMSupervisor` falls back).

### 1.2 How many AI calls a mission costs (why 20 per day is nothing)

| Step | Calls | Notes |
| --- | --- | --- |
| Planning a typed mission | 1 (2 if the first answer is invalid) | Scenario plans skip it |
| Each supervisor checkpoint | 1 (2 if invalid) | `drone_search`, `drone_scan`, `rover_inspect`, `ask_human`, `camera_locate`, every failed step, every typed change. **Scenario plans use it too when a key is set** |
| `rover_search` | 1 per 45° turn, up to 9 | Vision |
| `rover_approach` | 1 per step, up to 15 | Vision |
| `rover_inspect`, `rover_check_grip` | 1 each | Vision |
| `camera_locate` | 1 per frame tried | Vision (overhead) |

A full recovery on the real floor is roughly **15–35 calls**; on mocks (`start.py demo`) only planner + supervisor
calls hit Gemini (≈3–6), because mock perception reads the simulated world.

### 1.3 Problems this plan fixes

| # | Problem | Evidence |
| --- | --- | --- |
| P1 | One model for everything; the scarce robotics model is also spent on text reasoning | `GeminiLLM` uses one chain for all tags |
| P2 | Fallback models do not exist for this key | 404s above |
| P3 | A model that answered 429 is tried again on every call (and the 429's retry time is ignored) | `generate` loops from `_first` each call |
| P4 | `.env` is not loaded | no loader anywhere; `start.py` only passes `os.environ` on |
| P5 | Stale live test | `tests/test_live.py:41` |
| P6 | The operator cannot see whether the AI or the rules decided, which model answered, or how many calls are left | `/state.metrics` has only `llm_latency_ms_last` |
| P7 | A good AI-made plan cannot be kept and replayed without AI (for filming) | no write path for scenarios |
| P8 | `manager.cli` cannot answer a question (authorization, "search field B?"), so it hangs | `cli.py` has no answer prompt |

---

## 2. Goals and non-goals

**Goals:** the AI plans and re-plans live with a working model; it adapts plans ("skip field A", "check the base
first") and composes new sequences from the existing actions; good results can be saved as scenarios; the operator
always sees what the AI did and whether it is available; quota is spent where it matters.

**Non-goals (decided; do not re-open inside a WP):**
- **No LangGraph or other agent framework.** The manager already is a state graph (plan → approve → run →
  checkpoint → re-plan) with ~380 tests; a rewrite on pitch day adds risk and nothing the jury can see.
- **No AI that edits code, config, the arena, safety values or existing scenario files.**
- **No new robot actions in this plan** (the vocabulary in `tools.py` stays as is).
- **No automatic function calling.** The executor keeps calling tools itself (approval and Stop between steps).

### 2.1 What the AI may and may not change (the contract every WP must keep)

| The AI MAY | The AI MAY NOT |
| --- | --- |
| Propose a plan from the action vocabulary | Run anything without the human's Approve (except a typed change while running, which is the human's own instruction: `CHANGE_NEEDS_APPROVAL=0`) |
| Replace the *pending* steps at a checkpoint, after a failure, or on a typed change | Touch steps that already ran, the geofences, caps, mentor or authorization gates |
| Ask the human a question (`ask_human`) | Answer its own authorization question |
| Have its plan saved as a **new** scenario file **when the human clicks Save** | Overwrite or delete a scenario; write anywhere but `scenarios/<new-slug>.yaml` |
| Write a one-line reason the human sees | Change `arena*.json`, `config.py`, `.env`, or any code |

---

## 3. Work packages

Order: **AI-1 → AI-2 → AI-3 → AI-4 → AI-5 → AI-6**; AI-7 is a stretch. AI-1…AI-4 are what makes the key useful today.

### AI-1  Load `.env` safely (P4) — S

- New `manager/envfile.py`: `load_env(path=ROOT/".env") -> list[str]` reads `KEY=VALUE` lines (ignore blanks,
  `#` comments, optional `export ` prefix, strip matching quotes), sets **only keys not already in `os.environ`**,
  returns the key **names** it set. Never prints or logs values. No new dependency.
- Call it at the top of `start.py main()` (before `missing()`/`plan()`; children inherit the environment) and in
  `manager/runtime.build_controller()` (covers `python -m manager.server` / `manager.cli` started by hand).
- `start.py summary()`: replace the "no GEMINI_API_KEY" line with `AI          key loaded from .env` /
  `key from environment` / `no key: built-in plans and rules only`.
- **Tests** (`tests/test_envfile.py`): parses the forms above; does not override an existing variable; a missing
  file is fine; the returned list holds names only.
- **Done when:** `unset GEMINI_API_KEY; .venv/bin/python start.py demo` reports the key loaded and the dashboard
  plans with Gemini.

### AI-2  Model routing by role, quota memory, working fallbacks (P1, P2, P3) — M

- **Config** (`manager/config.py`), all env-overridable, comma-separated lists:
  - `models_reason` (env `MODEL_REASON`), default `("gemini-3.8-flash", "gemini-flash-latest", "gemini-3.5-flash-lite")`
    for tags `plan`, `supervisor`.
  - `models_vision` (env `MODEL_VISION`), default `("gemini-robotics-er-2-preview", "gemini-3.8-flash", "gemini-flash-latest")`
    for tags `point`, `inspect`, `holding`, `overhead_point`.
  - Keep `MODEL` working: if set, it is put first in **both** lists (backwards compatible with README commands).
  - Remove the dead defaults (`gemini-robotics-er-1.5-preview`, `gemini-2.5-flash`) from `model_fallbacks`; keep the
    field only as an alias that, if set via env, is appended to both lists.
- **`GeminiLLM`** (`manager/llm.py`):
  - `ROLE = {"plan": "reason", "supervisor": "reason", "point": "vision", "inspect": "vision", "holding": "vision",
    "overhead_point": "vision"}`; unknown tags → `reason`. Each role has its own chain and its own sticky index.
  - **Model health memory** (shared across roles, thread-safe): on a 429, parse the retry delay from the error
    (`retryDelay: '5395s'` or "retry in 1h29m…"; if unparseable use 15 min) and skip that model until then; on a
    404 / "not found" / "no longer available", skip it for the rest of the process. Skipped models are not called.
  - When every model of a role is skipped, raise `LLMError("all <role> models unavailable: …")` **immediately**
    (no network call), so the supervisor falls back to rules without waiting.
  - Record per model: calls, failures, last error (first 160 chars), `cooling_until` (epoch seconds) → exposed via
    a `status()` method.
- **Telemetry:** `runtime.build_controller` passes a callback; the controller's monitor (or the callback) writes
  `store.metrics["llm"] = {"reason": {"model": <last that answered>, "calls": n}, "vision": {...},
  "models": {name: {"calls", "failures", "cooling_until", "error"}}, "last_source": "ai"|"rules"}`. Additive to
  `/state` only.
- **`last_source`:** `LLMSupervisor.decide` and `Planner.plan` set it (`"ai"` when the LLM answer was used,
  `"rules"`/`"fallback"` otherwise) through a small hook on the store (`store.set_metric`).
- **Tests** (`tests/test_llm.py`, with a fake `client.models.generate_content`): plan tags use the reason chain and
  vision tags the vision chain; a 429 with `retryDelay: '120s'` skips that model for 120 s then retries it; a 404
  skips it for good; all-skipped raises without calling; `MODEL=x` puts `x` first in both chains; the key never
  appears in `status()` or logs.
- **Done when:** with ER-2 exhausted, `start.py demo` plans and supervises via `gemini-3.8-flash` with no failed
  calls in the log after the first 429, and the dashboard (AI-3) shows ER-2 as cooling down.

### AI-3  Show the AI's state on the dashboard (P6) — S

- `manager/static/app.js` + `index.html`: a compact **AI chip** in the top bar (next to the connection lights):
  - `AI · gemini-3.8-flash · 14 calls` when the reason role answered recently;
  - amber `AI · vision cooling down 1h29m` when the vision chain is fully skipped;
  - grey `AI off · built-in plans` without a key (`metrics.llm` absent).
  - Tooltip: per-model calls / failures / cooling time from `metrics.llm.models`.
- In the plan card, label the latest decision source: "AI decided" vs "Rules decided (AI unavailable)" from
  `metrics.llm.last_source`. The supervisor already appends "(rule-based: …)" to its reason; keep that text.
- Must keep working when `metrics.llm` is missing (older server, no key). No layout shift.
- **Tests:** `tests/test_dashboard_files.py` (static checks for the new ids); one `pytest -m ui` case that
  injects `metrics.llm` and checks the chip text.

### AI-4  Fix and extend the live tests, with a call budget (P5) — S

- `tests/test_live.py`: the inspect test checks a **superset**: `{"answer", "label", "confidence", "grippable",
  "category", "hazardous"} <= set(out)`, and that `category` is one of the four allowed values.
- Add `test_planner_handles_a_typed_change` (plan the demo mission, then re-plan with "skip field A and search
  field B instead": the new plan's `drone_search` targets `$zone.field_b`).
- Add `test_supervisor_decides_after_an_inspection` (feed a finished `rover_inspect` with `category: own_drone`;
  expect `replan` whose remaining plan delivers to `$home`).
- Live tests use `models_reason` for text and `models_vision` for images, so a run costs ≈4 reason calls and
  1 vision call. Print the model that answered each call.
- **Done when:** `pytest -m live` passes with ER-2 still cooling down (vision falls through to Flash).

### AI-5  Save a plan as a scenario (P7) — M

Purpose: let the AI be creative before filming, then replay the exact plan with no AI involved.

- `manager/scenarios.py`: `save_scenario(name, title, mission, steps: list[StepDraft], targets) -> Path`.
  - `name` must match the slug regex and **must not exist** (`FileExistsError` → HTTP 409). Never overwrites.
  - `validate_drafts(steps)` must return no problems (→ HTTP 422 with the problems).
  - Writes `scenarios/<name>.yaml` atomically (temp file + `os.replace`), with a header comment
    `# saved from mission <id> (<plan source>) on <timestamp>`; keys `title`, `mission`, `targets`, `plan`
    (steps as flat dicts, `why` kept, statuses/results dropped).
  - `targets`: the targets the detector currently holds (`ctx.detector.targets` as `ScenarioTarget`), so a replay
    puts the wreck where it was; empty list if there is no detector.
- `Controller.save_current_plan(name, title)`: takes `store.plan` (all steps, original order) and
  `store.mission.text`; allowed in `awaiting_approval`, `done`, `failed`, `stopped`; `Conflict` while running.
- `server.py`: `POST /scenarios {"name", "title"}` → `{"ok": true, "name": ...}`; `GET /scenarios` then lists it.
- Dashboard: a **Save as scenario** button in the plan card (enabled when a plan exists and nothing runs) →
  prompt for name and title → POST → refresh the scenario list; show the 409/422 reason inline.
- **Tests** (`tests/test_scenarios.py`): save → load round trip equals the plan; existing name → 409; bad slug →
  422/404; invalid plan → 422; saving while running → 409; the saved scenario runs to `done` on mocks.

### AI-6  CLI answers questions (P8) — S

- `manager/cli.py`: when `store.mission.pending_question` is set and was not shown yet, print it and read a line
  from stdin (`input("answer> ")`), then `ctrl.change(answer)`. Flag `--answer yes|no` answers automatically
  (testing); `--auto-approve` alone keeps today's behaviour for plans.
- **Tests:** `tests/test_cli.py` runs `t3_recover_hostile` with `--auto-approve --answer yes` to `done` on mocks.

### AI-7 (stretch)  Light plan edits without the AI — M

For "cosmetic" edits when the AI is off or slow: in `awaiting_approval`, each step row gets **Remove** and
**Move up / down**; the edited list is sent to `POST /plan` (re-validated server-side, new plan version, source
`manual`). Nothing else changes. Only if AI-1…AI-6 are done.

---

## 4. How to test, module by module, then the whole thing

```bash
.venv/bin/python -m pytest                                      # all mock tests, no key needed (~40 s)
.venv/bin/python -m pytest tests/test_llm.py tests/test_planning.py -v      # AI-2 routing / planner, no network
.venv/bin/python -m pytest -m live -s                           # real Gemini, ~5 calls (AI-4)
.venv/bin/python -m pytest -m ui                                # dashboard in headless Chromium (AI-3, AI-5)
.venv/bin/python start.py demo                                  # whole app, simulated robots, real AI planning
```

Manual acceptance on `start.py demo` (each costs a few reason calls, no vision calls):

1. Type "A hostile drone crashed in field A. Find it and move it to the disposal zone." → plan appears, AI chip
   shows the reason model, Approve → runs to the authorization question → Yes → done.
2. Before approving, change: "search field B instead" → new plan version targets field B.
3. While running, change: "after the inspection bring it home instead" → the remaining plan changes.
4. Save the plan as `ai_field_b_recovery` → it appears under Scenarios → load it with the AI chip off (unset key,
   restart) → it runs identically.
5. Kill the network (Wi-Fi off) mid-mission → rules take over, chip says so, mission still ends cleanly.

---

## 5. Risks

| Risk | Mitigation |
| --- | --- |
| Free-tier daily limits on the Flash models too | Per-model cooldowns (AI-2) spread load over three models; mock perception in demo mode spends no vision calls; scenarios (AI-5) replay without AI |
| A model returns valid JSON with a bad plan | Already caught by `validate_drafts`; human Approve; safety in code |
| Model names change again | Lists are env-configurable (`MODEL_REASON`, `MODEL_VISION`); `GeminiLLM` skips 404s for good |
| Vision quality differs between ER-2 and Flash | Vision falls back to Flash only while ER-2 cools down; the AI chip says which model answered |
| Key leaks | `.env` stays git-ignored; values are never logged; `runs/` logs prompts and responses only |

## 6. Out of scope here: the network

The rover link problems are a Wi-Fi path problem (see integration-plan.md: EP link stability). A venue 5G router
would help only as a proper access point instead of the phone hotspot; ask the mentors. Nothing in this plan
depends on it.
