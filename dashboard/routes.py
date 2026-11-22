"""Dashboard pages and the platform API (PRD §17). Renders engine output, records decisions."""

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from dashboard import auth, repo, ui
from dashboard.db import DbError

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
ui.install(templates)


class LoginRequired(Exception):
    pass


def db_of(request: Request):
    return request.app.state.db_factory()


def user_of(request: Request) -> auth.User:
    user = auth.read_session(request.cookies.get(auth.COOKIE))
    if not user:
        raise LoginRequired()
    return user


def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    user = auth.read_session(request.cookies.get(auth.COOKIE))
    return templates.TemplateResponse(request, name, {"user": user, **ctx}, status_code=status)


def back_to(request_id: str, notice: str = "", error: str = "") -> RedirectResponse:
    url = f"/requests/{quote(request_id)}"
    if notice:
        url += "?notice=" + quote(notice)
    elif error:
        url += "?error=" + quote(error)
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- login
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return page(request, "login.html", next=_safe_next(next), error=None)


@router.post("/login")
async def login(request: Request):
    form = await request.form()
    target = _safe_next(str(form.get("next", "/")))
    user = auth.check_login(str(form.get("email", "")), str(form.get("password", "")))
    if not user:
        return page(request, "login.html", status=401, next=target, error="Wrong email or password.")
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(auth.COOKIE, auth.make_session(user), max_age=auth.SESSION_TTL_S,
                        httponly=True, secure=request.url.scheme == "https", samesite="lax")
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE)
    return response


def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


# ---------------------------------------------------------------- requests
@router.get("/", response_class=HTMLResponse)
def request_list(request: Request):
    user_of(request)
    return page(request, "list.html", requests=repo.list_requests(db_of(request)))


@router.get("/requests/new", response_class=HTMLResponse)
def new_request_form(request: Request):
    if not user_of(request).can_approve:
        return page(request, "error.html", status=403, message="Your account is read-only.")
    return page(request, "new.html", request_id=repo.new_request_id(), errors=[], values={})


def _writer_or_none(request: Request) -> auth.User | None:
    user = user_of(request)
    return user if user.can_approve else None


@router.post("/requests")
async def create_request(request: Request):
    """Form post from the dashboard, or JSON (PRD §7 input) from a script with API_TOKEN."""
    is_json = request.headers.get("content-type", "").startswith("application/json")
    if is_json:
        if auth.api_token_ok(request.headers.get("authorization")):
            creator = "api"
        else:
            writer = _writer_or_none(request)
            if not writer:
                return JSONResponse({"error": "read-only account"}, status_code=403)
            creator = writer.email
        try:
            data = repo.RequestInput.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            return JSONResponse({"error": _errors(exc)}, status_code=422)
        request_id, created = repo.create_request(db_of(request), data, creator)
        return JSONResponse({"request_id": request_id, "created": created}, status_code=201 if created else 200)

    user = _writer_or_none(request)
    if not user:
        return page(request, "error.html", status=403, message="Your account is read-only.")
    form = dict(await request.form())
    try:
        data = repo.RequestInput.model_validate(_form_to_input(form))
    except (ValidationError, ValueError) as exc:
        return page(request, "new.html", request_id=form.get("request_id") or repo.new_request_id(),
                    errors=_errors(exc), values=form)
    request_id, created = repo.create_request(db_of(request), data, user.email)
    return back_to(request_id, notice="Request created." if created else "Request already existed.")


def _form_to_input(form: dict) -> dict:
    def text(name):
        value = str(form.get(name, "")).strip()
        return value or None

    def number(name, cast=float):
        value = text(name)
        if value is None:
            return None
        try:
            return cast(value)
        except ValueError:
            raise ValueError(f"{name.replace('_', ' ')} must be a number") from None

    dims = [number("size_l"), number("size_w"), number("size_h")]
    return {
        "request_id": text("request_id"),
        "item": text("item"),
        "spec": {
            "ply": number("ply", int),
            "size_in": dims if all(d is not None for d in dims) else None,
            "material": text("material"),
            "printing": text("printing"),
        },
        "quantity_per_month": number("quantity_per_month", int),
        "delivery_location": text("delivery_location"),
        "needed_by": text("needed_by"),
        "budget_inr_per_unit": number("budget_inr_per_unit"),
        "free_text": text("free_text"),
    }


def _errors(exc) -> list[str]:
    if isinstance(exc, ValidationError):
        out = []
        for e in exc.errors():
            where = ".".join(str(p) for p in e["loc"])
            msg = e["msg"].removeprefix("Value error, ")
            out.append(f"{where}: {msg}" if where else msg)
        return out
    return [str(exc)]


@router.get("/requests/{request_id}", response_class=HTMLResponse)
def request_detail(request: Request, request_id: str, notice: str = "", error: str = ""):
    user_of(request)
    view = repo.get_request_view(db_of(request), request_id)
    if view is None:
        return page(request, "error.html", message=f"No request {request_id}.", status=404)
    return page(request, "request.html", r=view, toast_notice=notice, toast_error=error, poll=_should_poll(view))


@router.get("/requests/{request_id}/live", response_class=HTMLResponse)
def request_live(request: Request, request_id: str):
    """htmx partial: re-rendered every few seconds while the engine is working."""
    user_of(request)
    view = repo.get_request_view(db_of(request), request_id)
    if view is None:
        return HTMLResponse("", status_code=404)
    return page(request, "_live.html", r=view, poll=_should_poll(view))


def _should_poll(view) -> bool:
    """Stop refreshing when nothing will change without a human, so an
    approval form being filled in is never re-rendered underneath the user."""
    waiting_on_human = any(a["status"] == "pending_approval" for a in view["actions"])
    return view["state"] not in repo.TERMINAL_STATES and not waiting_on_human


@router.get("/api/requests")
def api_list(request: Request):
    _api_user(request)
    return repo.list_requests(db_of(request))


@router.get("/api/requests/{request_id}")
def api_detail(request: Request, request_id: str):
    _api_user(request)
    view = repo.get_request_view(db_of(request), request_id)
    if view is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return view


def _api_user(request: Request) -> str:
    if auth.api_token_ok(request.headers.get("authorization")):
        return "api"
    return user_of(request).email


def approver_of(request: Request, request_id: str):
    """The logged-in approver, or a redirect explaining why this user cannot decide."""
    user = user_of(request)
    if not user.can_approve:
        return None, back_to(request_id, error="Your account is read-only; only the approver can decide.")
    return user, None


# ---------------------------------------------------------------- approvals
@router.post("/approvals/{approval_id}")
async def decide(request: Request, approval_id: int):
    """One decision. Approve needs the fingerprint of the email shown; reject needs a reason.
    Session login only: an API token can never approve an irreversible action."""
    form = await request.form()
    request_id = str(form.get("request_id", ""))
    user, refusal = approver_of(request, request_id)
    if refusal:
        return refusal
    decision = {"approve": "approved", "reject": "rejected"}.get(str(form.get("decision")), "")
    try:
        repo.decide_approval(db_of(request), approval_id, decision, user.email,
                             str(form.get("reason", "")), str(form.get("fingerprint", "")))
    except repo.DecisionError as exc:
        return back_to(request_id, error=str(exc))
    return back_to(request_id, notice=f"Recorded: {decision} by {user.email}.")


@router.post("/requests/{request_id}/rfq-decisions")
async def decide_rfqs(request: Request, request_id: str):
    """Per-supplier RFQ tick-boxes: ticked are approved, unticked are rejected with the typed reason."""
    user, refusal = approver_of(request, request_id)
    if refusal:
        return refusal
    form = await request.form()
    ticked = {int(v) for v in form.getlist("approve") if str(v).isdigit()}
    reason = str(form.get("reason", "")).strip()
    view = repo.get_request_view(db_of(request), request_id)
    if view is None:
        return back_to(request_id, error="request not found")
    # Only drafts the approver could actually read are decided; ones without text stay pending.
    pending = [a for a in view["actions"]
               if a["type"] == "send_rfq" and a["status"] == "pending_approval" and (a["email_body"] or "").strip()]
    if not pending:
        return back_to(request_id, error="No RFQ drafts are ready to decide.")
    if any(a["id"] not in ticked for a in pending) and not reason:
        return back_to(request_id, error="Unticked suppliers are rejected, which needs a typed reason. Nothing was recorded.")
    done, problems = [], []
    for a in pending:
        decision = "approved" if a["id"] in ticked else "rejected"
        try:
            repo.decide_approval(db_of(request), a["id"], decision, user.email,
                                 reason if decision == "rejected" else None,
                                 str(form.get(f"fingerprint_{a['id']}", "")))
            done.append(f"{a['supplier_name']}: {decision}")
        except repo.DecisionError as exc:
            problems.append(f"{a['supplier_name']}: {exc}")
    if problems:
        return back_to(request_id, error="; ".join(problems + done))
    return back_to(request_id, notice="Recorded " + "; ".join(done) + f" (by {user.email}).")


# ---------------------------------------------------------------- error handling
def install(app):
    app.include_router(router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc):
        if request.url.path.startswith("/api/") or request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"error": "login required"}, status_code=401)
        if request.headers.get("hx-request"):
            return HTMLResponse("", status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)

    @app.exception_handler(DbError)
    async def _db_error(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(exc)}, status_code=503)
        return page(request, "error.html", message=str(exc), status=503)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response
