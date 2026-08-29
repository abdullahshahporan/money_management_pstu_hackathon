"""Account, recipient lookup and statement endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from apps.api.dependencies import ContainerDep, CurrentUser, RequestId, rate_limit
from apps.api.schemas import PHONE_PATTERN
from platform_.kernel.errors import NotFoundError
from platform_.web.envelope import success_response

router = APIRouter(tags=["accounts"])


@router.get("/accounts/me", summary="Authoritative account summary")
def get_my_account(container: ContainerDep, user: CurrentUser, request_id: RequestId):
    """Return the balance from the primary, never from a cache or replica.

    Spec 25/26: a balance shown to the person about to spend it must be
    authoritative. Redis caches profiles here, not money.
    """

    def query(session):  # noqa: ANN001, ANN202
        account = container.accounts.get_by_user_id(session, user.user_id)
        if account is None:
            raise NotFoundError("Account not found.")
        record = container.users.get_by_id(session, user.user_id)
        return {
            "accountId": account.id,
            "displayName": record.display_name if record else "",
            "phone": record.phone if record else "",
            "balanceMinor": account.balance.minor,
            "currency": account.balance.currency.code,
            "status": account.status,
        }

    return success_response(
        container.unit_of_work.run(query), request_id=request_id
    )


@router.get(
    "/accounts/lookup",
    summary="Confirm a recipient before sending",
    dependencies=[Depends(rate_limit("lookup"))],
)
def lookup_recipient(
    phone: Annotated[str, Query(pattern=PHONE_PATTERN)],
    container: ContainerDep,
    user: CurrentUser,
    request_id: RequestId,
):
    """Resolve a phone number to a display name.

    This is the safety step that stops money going to a mistyped number: the
    sender sees who they are about to pay before they confirm. Exact match
    only - a prefix search would let anyone enumerate the user directory.
    """

    def query(session):  # noqa: ANN001, ANN202
        found = container.users.search(session, phone=phone, exclude_user_id=user.user_id)
        if found is None:
            raise NotFoundError("No active user found with that phone number.")
        return found

    return success_response(container.unit_of_work.run(query), request_id=request_id)


@router.get(
    "/transactions",
    summary="Paginated account statement",
    dependencies=[Depends(rate_limit("read"))],
)
def list_transactions(
    container: ContainerDep,
    user: CurrentUser,
    request_id: RequestId,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=26)] = None,
):
    """Cursor-paginated history, newest first.

    ``nextCursor`` is the last entry id. Clients pass it back to continue;
    there is deliberately no page-number API, because offset pagination
    degrades linearly with depth and also skips or repeats rows when new
    transfers arrive mid-scroll.
    """

    def query(session):  # noqa: ANN001, ANN202
        account = container.accounts.get_by_user_id(session, user.user_id)
        if account is None:
            raise NotFoundError("Account not found.")
        # Fetch one extra row to discover whether another page exists without
        # running a second COUNT query.
        rows = container.statements.list_entries(
            session, account_id=account.id, limit=limit + 1, cursor=cursor
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        return {
            "entries": page,
            "nextCursor": page[-1]["entryId"] if has_more and page else None,
            "hasMore": has_more,
        }

    return success_response(container.unit_of_work.run(query), request_id=request_id)
