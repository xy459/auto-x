from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .contracts import AdminServices, JsonObject
from .dependencies import get_services
from .schemas import AccountCreate, AccountUpdate, BrowserBatchRequest

router = APIRouter(tags=["账户与浏览器"])


@router.get("/accounts")
async def list_accounts(services: AdminServices = Depends(get_services)) -> JsonObject:
    return {"accounts": await services.backend.list_accounts()}


@router.get("/accounts/browser/status")
async def browser_status(services: AdminServices = Depends(get_services)) -> JsonObject:
    return await services.backend.browser_status()


@router.post("/accounts/browser/batch")
async def browser_batch(
    body: BrowserBatchRequest,
    services: AdminServices = Depends(get_services),
) -> JsonObject:
    return await services.backend.browser_batch(body.account_ids, body.action)


@router.post("/accounts")
async def create_account(
    body: AccountCreate, services: AdminServices = Depends(get_services)
) -> JsonObject:
    return {"account": await services.backend.create_account(body.model_dump())}


@router.get("/accounts/{account_id}")
async def get_account(
    account_id: str, services: AdminServices = Depends(get_services)
) -> JsonObject:
    account = await services.backend.get_account(account_id)
    if account is None:
        raise HTTPException(404, "账户不存在")
    return {"account": account}


@router.put("/accounts/{account_id}")
async def update_account(
    account_id: str,
    body: AccountUpdate,
    services: AdminServices = Depends(get_services),
) -> JsonObject:
    account = await services.backend.update_account(
        account_id, body.model_dump(exclude_unset=True)
    )
    if account is None:
        raise HTTPException(404, "账户不存在")
    return {"account": account}


@router.post("/accounts/{account_id}/browser/{action}")
async def browser_action(
    account_id: str,
    action: str,
    services: AdminServices = Depends(get_services),
) -> JsonObject:
    if action not in {"open", "close", "restart"}:
        raise HTTPException(400, "浏览器操作只支持 open、close 或 restart")
    return await services.backend.browser_action(account_id, action)
