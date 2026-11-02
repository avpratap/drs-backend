import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clientip import client_ip
from app.core.config import settings
from app.core.db import get_db
from app.core.phone import is_indian_mobile, normalize_phone
from app.deps.auth import get_current_user
from app.models.user import User
from app.schemas.auth import (
    DevOTPOut,
    OTPRequest,
    OTPRequestResponse,
    OTPVerify,
    RefreshRequest,
    TokenPair,
    UserOut,
)
from app.schemas.common import DetailOut
from app.services import auth as auth_service
from app.services import otp as otp_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_meta(request: Request) -> dict:
    return {
        "device_name": request.headers.get("user-agent", "")[:255] or None,
        "user_agent": request.headers.get("user-agent", "")[:512] or None,
        "ip": client_ip(request),
    }


# exclude_none: in prod (or once an SMS was sent) the response carries NO dev_otp key at
# all, not `"dev_otp": null` — a client testing for the key's presence must never show a
# "dev code" box in production.
@router.post(
    "/otp/request",
    response_model=OTPRequestResponse,
    response_model_exclude_none=True,
    operation_id="request_otp",
)
async def request_otp(data: OTPRequest, request: Request):
    phone = normalize_phone(data.phone)
    if not is_indian_mobile(phone):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid Indian mobile number")
    ip = client_ip(request)
    try:
        dev_otp = await otp_service.request_otp(phone, ip=ip)
    except otp_service.UseLatestCode:
        # Capped, but the last code sent to this number is still valid: not an error.
        return OTPRequestResponse(
            detail="An OTP was already sent to this number; use the latest code you received"
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    return OTPRequestResponse(detail="OTP sent", dev_otp=dev_otp)


@router.post("/otp/verify", response_model=TokenPair, operation_id="verify_otp")
async def verify_otp(data: OTPVerify, request: Request, db: AsyncSession = Depends(get_db)):
    phone = normalize_phone(data.phone)
    if not is_indian_mobile(phone):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid Indian mobile number")
    try:
        ok = await otp_service.verify_otp(phone, data.otp, ip=client_ip(request))
    except ValueError as exc:  # per-IP failed-verify cap
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired OTP")
    user = await auth_service.get_or_create_user(db, phone)
    if user.status != "active":
        # Don't mint (unusable) tokens + a session row for a suspended/erased account.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is not active")
    access, refresh, _ = await auth_service.issue_tokens(db, user, **_client_meta(request))
    await db.commit()
    return TokenPair(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenPair, operation_id="refresh_token")
async def refresh_token(data: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        _user, access, refresh = await auth_service.rotate_refresh(
            db, data.refresh_token, **_client_meta(request)
        )
    except auth_service.AuthError as exc:
        await db.commit()  # persist family revocation if reuse was detected
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    await db.commit()
    return TokenPair(access_token=access, refresh_token=refresh)


@router.post("/logout", operation_id="logout", response_model=DetailOut)
async def logout(data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    await auth_service.revoke_family_by_refresh(db, data.refresh_token)
    await db.commit()
    return {"detail": "logged out"}


@router.get("/me", response_model=UserOut, operation_id="get_me")
async def me(user: User = Depends(get_current_user)):
    return user


# ---------------------------------------------------------------------------
# DEV/STAGING-ONLY OTP lookup. A testing aid for when no SMS/WhatsApp provider can
# reach a tester's number (e.g. a Twilio trial that only delivers to verified numbers).
# It DOES NOT EXIST in prod: every path returns 404 when ENV == "prod", and the plaintext
# code it reads is never even written to Redis in prod. It is protected by a shared secret
# (DEV_OTP_TOKEN) so it is not open to the world on a public staging host.
# ---------------------------------------------------------------------------
def _assert_dev_otp_enabled() -> None:
    # 404 (not 403) in prod / when unconfigured: the feature should look absent, not present.
    if settings.ENV == "prod" or not settings.DEV_OTP_TOKEN:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def _check_dev_token(token: str | None) -> None:
    expected = settings.DEV_OTP_TOKEN
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid dev token")


@router.get("/dev/otp/{phone}", response_model=DevOTPOut, operation_id="dev_peek_otp")
async def dev_peek_otp(phone: str, x_dev_token: str | None = Header(default=None)):
    """Return the current OTP for a phone (dev/staging only, secret-gated). 404 in prod."""
    _assert_dev_otp_enabled()
    _check_dev_token(x_dev_token)
    norm = normalize_phone(phone)
    if not is_indian_mobile(norm):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid Indian mobile number")
    otp, ttl = await otp_service.peek_otp(norm)
    if not otp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active code for this number")
    return DevOTPOut(phone=norm, otp=otp, ttl_seconds=ttl)


@router.get("/dev/otp", include_in_schema=False)
async def dev_otp_page():
    """Tiny self-contained lookup page (dev/staging only). 404 in prod."""
    _assert_dev_otp_enabled()
    return HTMLResponse(_DEV_OTP_PAGE)


_DEV_OTP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DRS · OTP lookup (dev)</title>
<style>
  :root{--green:#0D4C3C;--gold:#C5A253;--cream:#F4EADE;--ink:#1E1A1E;}
  *{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
  body{margin:0;min-height:100vh;display:flex;align-items:flex-start;justify-content:center;
       background:var(--cream);color:var(--ink);padding:24px 16px}
  .card{width:100%;max-width:420px;background:#fff;border-radius:16px;padding:24px;
        box-shadow:0 8px 30px rgba(0,0,0,.08);margin-top:6vh}
  h1{margin:0 0 4px;font-size:20px;color:var(--green)}
  p.sub{margin:0 0 18px;font-size:13px;color:#6b6b6b}
  label{display:block;font-size:12px;font-weight:600;margin:12px 0 6px}
  input{width:100%;padding:12px 14px;border:1px solid #ddd;border-radius:10px;font-size:15px}
  input:focus{outline:none;border-color:var(--green)}
  button{width:100%;margin-top:18px;padding:13px;border:0;border-radius:10px;cursor:pointer;
         background:var(--green);color:var(--cream);font-size:15px;font-weight:600}
  button:active{transform:translateY(1px)}
  .out{margin-top:18px;text-align:center;display:none}
  .code{font-size:40px;letter-spacing:8px;font-weight:700;color:var(--green);
        padding:14px;background:var(--cream);border-radius:12px;border:1px dashed var(--gold)}
  .ttl{font-size:12px;color:#6b6b6b;margin-top:6px}
  .err{color:#b00020;font-size:13px;margin-top:14px;text-align:center;display:none}
  .badge{display:inline-block;font-size:10px;font-weight:700;color:var(--gold);
         border:1px solid var(--gold);border-radius:6px;padding:2px 6px;vertical-align:middle}
</style></head>
<body><div class="card">
  <h1>DRS OTP lookup <span class="badge">DEV</span></h1>
  <p class="sub">Enter a tester's number to read the current login code. Testing aid only &mdash; not available in production.</p>
  <label for="tok">Dev token</label>
  <input id="tok" type="password" placeholder="DEV_OTP_TOKEN" autocomplete="off">
  <label for="ph">Phone number</label>
  <input id="ph" type="tel" inputmode="numeric" placeholder="e.g. 9000000001" autocomplete="off">
  <button id="go">Get code</button>
  <div class="out" id="out"><div class="code" id="code"></div><div class="ttl" id="ttl"></div></div>
  <div class="err" id="err"></div>
</div>
<script>
  const $=id=>document.getElementById(id);
  try{const t=localStorage.getItem('drs_dev_otp_token');if(t)$('tok').value=t;}catch(e){}
  async function lookup(){
    const tok=$('tok').value.trim(), ph=$('ph').value.trim();
    $('out').style.display='none'; $('err').style.display='none';
    if(!tok||!ph){$('err').textContent='Enter both the dev token and a phone number.';$('err').style.display='block';return;}
    try{localStorage.setItem('drs_dev_otp_token',tok);}catch(e){}
    try{
      const base=location.pathname.replace(/\\/$/,'');
      const r=await fetch(base+'/'+encodeURIComponent(ph),{headers:{'X-Dev-Token':tok}});
      if(!r.ok){
        const m=r.status===401?'Invalid dev token.':r.status===404?'No active code for that number (ask them to request an OTP first).':'Lookup failed ('+r.status+').';
        $('err').textContent=m;$('err').style.display='block';return;
      }
      const d=await r.json();
      $('code').textContent=d.otp;
      $('ttl').textContent='valid ~'+Math.max(1,Math.round(d.ttl_seconds/60))+' min';
      $('out').style.display='block';
    }catch(e){$('err').textContent='Network error.';$('err').style.display='block';}
  }
  $('go').addEventListener('click',lookup);
  $('ph').addEventListener('keydown',e=>{if(e.key==='Enter')lookup();});
</script>
</body></html>"""
