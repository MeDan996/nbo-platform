"""Response helpers shared by the routers."""
from __future__ import annotations

from fastapi.responses import RedirectResponse

from app.templating import PageContext


def redirect(ctx: PageContext, url: str, status_code: int = 303) -> RedirectResponse:
    """Commit the request's work, then redirect.

    The request-scoped session commits in its dependency teardown, which runs
    once the handler has already returned its response. A 303 points the browser
    straight at a URL that reads the row we just wrote, and that follow-up
    request can arrive before the commit lands - which shows up as "Start exam"
    redirecting to a 404 for the attempt that was, a moment later, definitely
    there. Committing here makes the write durable before the redirect goes out.

    Use this for any redirect whose target reads state this request created.
    """
    ctx.db.commit()
    return RedirectResponse(url, status_code=status_code)
