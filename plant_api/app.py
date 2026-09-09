from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .config import Settings
from .domain import PlantAPIError
from .service import PlantDiaryService


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ConfirmationRequest(RequestModel):
    confirmed: bool = False


class FileMutationRequest(ConfirmationRequest):
    openai_file_id_refs: list[dict[str, str]] = Field(alias="openaiFileIdRefs", min_length=1)


class AddPhotosRequest(FileMutationRequest):
    date: Optional[str] = None
    type: str = "Состояние"
    comment: Optional[str] = None
    cover_file_id: Optional[str] = None


class EventRequest(ConfirmationRequest):
    type: str
    title: Optional[str] = None
    description: Optional[str] = None
    date: Optional[str] = None
    date_precision: str
    period: Optional[str] = None


class EventInput(RequestModel):
    type: str
    title: Optional[str] = None
    description: Optional[str] = None
    date: Optional[str] = None
    date_precision: str
    period: Optional[str] = None


class CreatePlantRequest(ConfirmationRequest):
    expected_plant_id: Optional[int] = Field(default=None, ge=1)
    name: str
    latin_name: Optional[str] = None
    cultivar: Optional[str] = None
    group: Optional[str] = None
    status: str = "Наблюдение"
    notes: Optional[str] = None
    date_added: Optional[str] = None
    openai_file_id_refs: list[dict[str, str]] = Field(
        default_factory=list, alias="openaiFileIdRefs"
    )
    photo_type: str = "Состояние"
    photo_comment: Optional[str] = None
    cover_file_id: Optional[str] = None
    acquisition_event: Optional[EventInput] = None


class IdentityRequest(ConfirmationRequest):
    expected_current_name: str
    name: Optional[str] = None
    latin_name: Optional[str] = None
    cultivar: Optional[str] = None


class CoverRequest(ConfirmationRequest):
    photo_record_id: Optional[str] = None
    web_key: Optional[str] = None
    allow_group_photo: bool = False


def create_app(
    settings: Optional[Settings] = None,
    service: Optional[PlantDiaryService] = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or PlantDiaryService(settings)
    app = FastAPI(
        title="Plant Diary GPT Action API",
        version=__version__,
        description="Private, confirmation-gated API for the plant diary.",
    )

    def require_api_key(x_plant_api_key: Optional[str] = Header(default=None)) -> None:
        if not settings.api_key:
            raise PlantAPIError("PLANT_API_KEY is not configured", status_code=503, code="not_configured")
        if not x_plant_api_key or not hmac.compare_digest(x_plant_api_key, settings.api_key):
            raise PlantAPIError("Invalid API key", status_code=401, code="unauthorized")

    @app.exception_handler(PlantAPIError)
    async def plant_api_error_handler(_request, exc: PlantAPIError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.get("/health", operation_id="healthCheck")
    def health() -> dict[str, Any]:
        return service.health()

    @app.get("/openapi-action.yaml", include_in_schema=False)
    def action_schema() -> Response:
        schema = Path(__file__).with_name("openapi-action.yaml").read_text(encoding="utf-8")
        if settings.public_api_url:
            schema = schema.replace("https://plant-api.example.com", settings.public_api_url)
        return Response(content=schema, media_type="application/yaml")

    @app.get("/plants/search", dependencies=[Depends(require_api_key)], operation_id="searchPlants")
    def search_plants(
        query: str = Query(min_length=1, max_length=200), limit: int = Query(default=10, ge=1, le=20)
    ) -> dict[str, Any]:
        return service.search_plants(query, limit=limit)

    @app.get("/plants/{plant_id}", dependencies=[Depends(require_api_key)], operation_id="getPlant")
    def get_plant(plant_id: int) -> dict[str, Any]:
        return service.get_plant(plant_id)

    @app.get(
        "/plants/{plant_id}/photos",
        dependencies=[Depends(require_api_key)],
        operation_id="listPlantPhotos",
    )
    def list_plant_photos(plant_id: int) -> dict[str, Any]:
        return service.list_plant_photos(plant_id)

    @app.post(
        "/plants/{plant_id}/photos",
        dependencies=[Depends(require_api_key)],
        operation_id="addPhotos",
    )
    def add_photos(plant_id: int, request: AddPhotosRequest) -> dict[str, Any]:
        return service.add_photos(plant_id, request.model_dump(by_alias=True, exclude_none=True))

    @app.post("/plants", dependencies=[Depends(require_api_key)], operation_id="createPlant")
    def create_plant(request: CreatePlantRequest) -> dict[str, Any]:
        return service.create_plant(request.model_dump(by_alias=True, exclude_none=True))

    @app.patch(
        "/plants/{plant_id}/identity",
        dependencies=[Depends(require_api_key)],
        operation_id="updatePlantIdentity",
    )
    def update_plant_identity(plant_id: int, request: IdentityRequest) -> dict[str, Any]:
        return service.update_identity(plant_id, request.model_dump(exclude_none=True))

    @app.put(
        "/plants/{plant_id}/cover",
        dependencies=[Depends(require_api_key)],
        operation_id="setPlantCover",
    )
    def set_plant_cover(plant_id: int, request: CoverRequest) -> dict[str, Any]:
        return service.set_cover(plant_id, request.model_dump(exclude_none=True))

    @app.post(
        "/plants/{plant_id}/cover/automatic",
        dependencies=[Depends(require_api_key)],
        operation_id="clearPlantCover",
    )
    def clear_plant_cover(plant_id: int, request: ConfirmationRequest) -> dict[str, Any]:
        return service.clear_cover(plant_id, request.model_dump())

    @app.post(
        "/plants/{plant_id}/events",
        dependencies=[Depends(require_api_key)],
        operation_id="addPlantEvent",
    )
    def add_plant_event(plant_id: int, request: EventRequest) -> dict[str, Any]:
        return service.add_event(plant_id, request.model_dump(exclude_none=True))

    return app


app = create_app()
