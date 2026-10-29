"""Admin-only views of the durable Soulseek peer scheduler ledger."""
import time
import os
from fastapi import APIRouter, Depends, HTTPException, Header
import httpx
from pydantic import BaseModel, Field
import peer_scheduler as scheduler


class SettingsUpdate(BaseModel):
    changes: dict


class PeerUpdate(BaseModel):
    blocked: bool | None = None
    disabled_until: int | None = Field(default=None, ge=0)
    rule_text: str | None = Field(default=None, max_length=2000)
    limits: dict | None = None


class RetryRequest(BaseModel):
    review: bool = False


def install(app, require_admin, resolve_linked_token=None):
    scheduler.initialize()
    router=APIRouter(prefix='/v1/admin/scheduler',dependencies=[Depends(require_admin)])

    @router.get('/snapshot')
    def snapshot():
        return scheduler.snapshot()

    @router.patch('/settings')
    def settings(body:SettingsUpdate):
        try:return scheduler.update_settings(body.changes)
        except ValueError as exc:raise HTTPException(400,str(exc)) from exc

    @router.get('/peers/{peer}/history')
    def history(peer:str):
        return {'peer':peer,'events':scheduler.peer_history(peer)}

    @router.patch('/peers/{peer}')
    def peer_update(peer:str,body:PeerUpdate):
        try:
            scheduler.set_peer(peer,**body.model_dump(exclude_unset=True))
        except ValueError as exc:raise HTTPException(400,str(exc)) from exc
        return scheduler.snapshot()

    @router.post('/jobs/{task_id}/review')
    def review(task_id:str):
        with scheduler.connect() as db:
            row=db.execute('SELECT state FROM batches WHERE task_id=?',(task_id,)).fetchone()
        if not row:raise HTTPException(404,'Unknown scheduler job')
        if row['state'] not in ('failed','partial','needs_review'):
            raise HTTPException(409,'Only failed jobs can be marked for review')
        scheduler.mark(task_id,'needs_review','Administrator requested review')
        return {'status':'needs_review'}

    @router.post('/jobs/{task_id}/retry')
    async def retry(task_id:str,x_droppedneedle_token:str=Header(default=''),authorization:str=Header(default='')):
        # Delegate to the native retry route so exact-version verification,
        # source switching and its attempt journal remain authoritative.
        with scheduler.connect() as db:
            row=db.execute('SELECT state FROM batches WHERE task_id=?',(task_id,)).fetchone()
        if not row:raise HTTPException(404,'Unknown scheduler job')
        if row['state'] not in ('failed','partial','needs_review'):
            raise HTTPException(409,'Only terminal jobs can be released for retry')
        token=x_droppedneedle_token.strip()
        if not token and resolve_linked_token and authorization:
            token=resolve_linked_token(authorization) or ''
        if not token:
            raise HTTPException(400,'Connect your DroppedNeedle account on the Account page before retrying')
        native_id=task_id.split(':',1)[0]
        try:
            async with httpx.AsyncClient(timeout=20,trust_env=False,follow_redirects=False) as client:
                result=await client.post(os.getenv('DROPPEDNEEDLE_URL','http://droppedneedle:8688').rstrip('/')+
                    '/api/v1/downloads/'+native_id+'/retry',
                    headers={'Authorization':'Bearer '+token})
            result.raise_for_status()
            payload=result.json()
            if not payload.get('success'):raise ValueError('Native retry not confirmed')
        except (httpx.HTTPError,ValueError,TypeError) as exc:
            raise HTTPException(502,'DroppedNeedle did not confirm retry; inspect before repeating') from exc
        scheduler.mark(task_id,'retry_ready','Native DroppedNeedle retry confirmed')
        return {'status':'retry_ready','new_task_id':payload.get('task_id')}

    app.include_router(router)
