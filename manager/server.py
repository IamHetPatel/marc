"""FastAPI app on :8002, the contract for the dashboard (docs/ai-manager-plan.md §9).

    POST /mission {"text"}   POST /approve {"plan_version"}   POST /change {"text"}   POST /stop
    POST /mentor {"present"} POST /reset                      GET /state              GET /images/{id}.jpg
    POST /manual {"robot","action","args"}   POST /plan {"title","steps"}   GET /scenarios   POST /scenarios/{name}/load
    GET /arena   GET /report                 GET /ui/ (the dashboard)       GET / -> /ui/
    GET /overhead.jpg (latest overhead webcam frame, annotated; 404 without OVERHEAD_CAM)

Run:  ROVER=mock DRONE=mock uvicorn manager.server:app --port 8002
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .controller import Conflict, Controller
from .models import StepDraft
from .scenarios import UnknownScenario, list_scenarios
from .tools import validate_step


STATIC_DIR = Path(__file__).resolve().parent / "static"       # the dashboard: plain HTML / JS, no build step


class MissionBody(BaseModel):
    text: str


class ApproveBody(BaseModel):
    plan_version: int


class ChangeBody(BaseModel):
    text: str


class MentorBody(BaseModel):
    present: bool


class PlanBody(BaseModel):
    title: str
    steps: list[StepDraft]


class ManualBody(BaseModel):
    robot: str
    action: str
    args: dict = {}


def create_app(controller: Optional[Controller] = None) -> FastAPI:
    owns = controller is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if owns:
            from .runtime import build_controller
            app.state.ctrl = build_controller()
        else:
            app.state.ctrl = controller
        app.state.ctrl.start()
        if owns and app.state.ctrl.cfg.scenario:
            try:
                app.state.ctrl.load_scenario(app.state.ctrl.cfg.scenario)
            except Exception:                    # noqa: BLE001  a bad SCENARIO must not keep the server from starting
                logging.getLogger("manager.server").exception("could not load scenario %r",
                                                              app.state.ctrl.cfg.scenario)
        yield
        app.state.ctrl.shutdown()

    app = FastAPI(title="FleetPilot AI manager", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def ctrl() -> Controller:
        return app.state.ctrl

    def guarded(fn, *args):
        try:
            return fn(*args)
        except Conflict as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui/")

    @app.get("/state")
    def state():
        return ctrl().store.to_json()

    @app.post("/mission")
    def mission(body: MissionBody):
        mission_id = guarded(ctrl().start_mission, body.text)
        return {"ok": True, "mission_id": mission_id}

    @app.post("/approve")
    def approve(body: ApproveBody):
        guarded(ctrl().approve, body.plan_version)
        return {"ok": True}

    @app.post("/change")
    def change(body: ChangeBody):
        guarded(ctrl().change, body.text)
        return {"ok": True}

    @app.post("/plan")
    def plan(body: PlanBody):
        mission_id = guarded(ctrl().submit_plan, body.title, body.steps, "manual")
        return {"ok": True, "mission_id": mission_id}

    @app.get("/scenarios")
    def scenarios():
        return list_scenarios()

    @app.post("/scenarios/{name}/load")
    def load(name: str):
        try:
            mission_id = guarded(ctrl().load_scenario, name)
        except UnknownScenario as e:
            raise HTTPException(status_code=404, detail=f"no scenario named {name!r}") from e
        return {"ok": True, "mission_id": mission_id}

    @app.post("/manual", status_code=202)
    def manual(body: ManualBody):
        if (msg := validate_step(body.action, body.robot, body.args)):
            raise HTTPException(status_code=422, detail=msg)
        guarded(ctrl().manual, body.action, body.args)
        return {"ok": True}

    @app.get("/report")
    def report():
        if ctrl().store.mission.id is None:
            raise HTTPException(status_code=404, detail="no mission yet")
        return ctrl().build_report()

    @app.get("/arena")
    def arena():
        return ctrl().base_ctx.arena.model_dump(exclude={"mock_objects"})

    @app.post("/stop")
    def stop():
        return {"ok": True, "robots": ctrl().stop()}

    @app.post("/mentor")
    def mentor(body: MentorBody):
        ctrl().set_mentor(body.present)
        return {"ok": True, "mentor_present": ctrl().store.mentor_present}

    @app.post("/reset")
    def reset():
        guarded(ctrl().reset)
        return {"ok": True}

    @app.get("/images/{image_id}.jpg")
    def image(image_id: str):
        data = ctrl().store.get_image(image_id)
        if data is None:
            raise HTTPException(status_code=404, detail="no such image")
        return Response(content=data, media_type="image/jpeg")

    @app.get("/overhead.jpg")
    def overhead():
        cam = ctrl().base_ctx.overhead
        data = cam.preview_jpeg() if cam is not None else None
        if data is None:
            raise HTTPException(status_code=404, detail="no overhead camera frame")
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    from .config import Config
    cfg = Config.from_env()
    uvicorn.run("manager.server:app", host=cfg.host, port=cfg.port)
