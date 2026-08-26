from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, status

from control_plane.app.modules.source_control import (
    SourceControlDependencies,
    SourceControlDependencyUnavailable,
    WebhookIdConflict,
    WebhookPayloadInvalid,
    WebhookReplayRejected,
    WebhookSignatureInvalid,
    ingest_signed_gitlab_webhook,
)


@dataclass(frozen=True, slots=True)
class SourceControlWebhookRuntime:
    dependencies: SourceControlDependencies


def create_webhook_router(
    runtime_provider: Callable[[], SourceControlWebhookRuntime],
) -> APIRouter:
    router = APIRouter(prefix="/webhooks/gitlab", tags=["source-control-webhooks"])

    @router.post("/{repository_id}", status_code=status.HTTP_202_ACCEPTED)
    async def receive_gitlab_webhook(repository_id: str, request: Request) -> dict[str, str]:
        try:
            runtime = runtime_provider()
            inbox = ingest_signed_gitlab_webhook(
                repository_id=repository_id,
                raw_body=await request.body(),
                headers=dict(request.headers),
                dependencies=runtime.dependencies,
            )
        except SourceControlDependencyUnavailable as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Source Control connector is unavailable",
            ) from error
        except (WebhookReplayRejected, WebhookSignatureInvalid) as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Webhook signature is invalid",
            ) from error
        except WebhookPayloadInvalid as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook payload is invalid",
            ) from error
        except WebhookIdConflict as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Webhook identity conflicts with a prior delivery",
            ) from error
        return {"inboxId": inbox.id, "state": inbox.state.value}

    return router
