"""FastAPI app on :8002, the contract for the dashboard (docs/ai-manager-plan.md §9).

    POST /mission {"text"}   POST /approve {"plan_version"}   POST /change {"text"}   POST /stop
    POST /mentor {"present"} POST /reset                      GET /state              GET /images/{id}.jpg

Run:  ROVER=mock DRONE=mock uvicorn manager.server:app --port 8002
"""
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .controller import Conflict, Controller


class MissionBody(BaseModel):
    text: str


class ApproveBody(BaseModel):
    plan_version: int


class ChangeBody(BaseModel):
    text: str


class MentorBody(BaseModel):
    present: bool


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

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    from .config import Config
    cfg = Config.from_env()
    uvicorn.run("manager.server:app", host=cfg.host, port=cfg.port)
