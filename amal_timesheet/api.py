# -*- coding: utf-8 -*-
"""
Amal Timesheet API
--------------------------
Same timesheet logic as the timesheet_kiosk app (v4), rebuilt for ONE shared
tablet used by MANY workers.

How it works (Amal Timesheet):

  1. The tablet itself signs in ONCE with a "device" account — an ERPNext
     user holding the "Kiosk Device" role (no desk access). That login is
     what keeps the tablet connected; it never identifies a worker.

  2. START / STOP on the WIH team screen is a plain tap next to the
     worker's name (tap_start / tap_stop). No face check, no PIN.

  3. Photos (back camera) are compulsory at these moments — the server
     refuses the Start / Stop without them:
       • START: the first Start of the day on each timesheet (WIH item),
         whoever does it. Later Starts that day on that item need no photo.
         Saved in Timesheet Entry.start_photo.
       • STOP: every Stop. Saved in Timesheet Entry.photo.
     Both are also attached to the Employee Timesheet and to the punch log.

  4. "My Hours / Supervisor" still uses the worker's personal PIN
     (verify_pin), which hands back a short-lived worker token held only in
     server memory. Those screens (dashboard, report, manual entries,
     submit) work exactly as before.

Data: this app reads and writes the SAME doctypes as timesheet_kiosk
(Employee Timesheet / Timesheet Entry / Timesheet Allocation / Work In Hand).
Each WIH has several items; each (WIH, item) has its own Employee Timesheet
(Employee Timesheet.item_code) and allocation is per item (Timesheet
Allocation.item). It ADDS three doctypes of its own (Amal Timesheet Worker /
Settings / Punch Log) and two fields on Timesheet Entry if missing
(photo, start_photo — see install.ensure_custom_fields).

Every function is called from the frontend as:
    POST /api/method/amal_timesheet.api.<function_name>
"""

import base64
import binascii
import json
import math
import secrets

import frappe
from frappe.auth import LoginManager
from frappe.utils import cint, flt, now_datetime, time_diff_in_hours, today
from frappe.utils.password import check_password, update_password

from amal_timesheet.utils import PIN_FIELD, validate_pin_format

TS_DOCTYPE = "Employee Timesheet"
ENTRY_DOCTYPE = "Timesheet Entry"
ALLOCATION_DOCTYPE = "Timesheet Allocation"
WORKER_DOCTYPE = "Amal Timesheet Worker"
LOG_DOCTYPE = "Amal Timesheet Punch Log"
SETTINGS_DOCTYPE = "Amal Timesheet Settings"
PAGE_SIZE = 10

# Item-level timesheets: each WIH has several items; each (WIH, item) pair has
# its own Employee Timesheet, and Timesheet Allocation is done per item.
TS_ITEM_FIELD = "item_code"      # on Employee Timesheet
ENTRY_PHOTO_FIELD = "photo"              # on Timesheet Entry: photo taken at Stop
ENTRY_START_PHOTO_FIELD = "start_photo"  # on Timesheet Entry: photo taken at the day's first Start
ALLOC_ITEM_FIELD = "item"        # on Timesheet Allocation

TIMESHEET_MANAGER_ROLE = "Timesheet Manager"
DEVICE_ROLE = "Kiosk Device"
# System Manager is allowed too, purely so an admin can test the kiosk from
# their own login before setting up a real device account.
DEVICE_ALLOWED_ROLES = {DEVICE_ROLE, "System Manager"}

CACHE_PREFIX = "amal_timesheet"
# The frontend looks for the words "session ended" to know it should go back
# to the name screen (rather than signing the whole device out).
SESSION_ENDED_MSG = "Session ended — please tap your name and enter your PIN again."
MAX_PHOTO_B64_CHARS = 600_000  # ~450 KB; the kiosk sends ~20-40 KB JPEGs
# Work-done photos are bigger (back camera, up to 1600 px) so the work is
# clearly visible; the tablet sends ~150-500 KB JPEGs.
MAX_WORK_PHOTO_B64_CHARS = 4_000_000  # ~3 MB
WORK_PHOTO_REQUIRED_MSG = "A photo of the work done is required to stop the timer. Please take the photo with the back camera."
# The tablet looks for "start photo is required" to open the camera and retry.
START_PHOTO_REQUIRED_MSG = "A start photo is required for the first entry of the day on this item. Please take the photo with the back camera."



# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _cache():
    # frappe.cache is an object in v15 (and still callable); a function in v14.
    c = frappe.cache
    return c() if callable(c) else c


def _opt(value, default):
    return default if value in (None, "") else cint(value)


def _settings():
    s = frappe.get_cached_doc(SETTINGS_DOCTYPE)
    return frappe._dict(
        worker_session_seconds=min(max(_opt(s.get("worker_session_seconds"), 60), 20), 600),
        max_pin_attempts=max(_opt(s.get("max_pin_attempts"), 5), 3),
        lockout_minutes=max(_opt(s.get("lockout_minutes"), 15), 1),
        require_photo=_opt(s.get("require_photo"), 0),
        allow_manual_entries=_opt(s.get("allow_manual_entries"), 1),
        one_timer_per_worker=_opt(s.get("one_timer_per_worker"), 1),
    )


def _public_settings():
    s = _settings()
    return {
        "worker_session_seconds": s.worker_session_seconds,
        "require_photo": bool(s.require_photo),
        "allow_manual_entries": bool(s.allow_manual_entries),
    }


def _full_name(user):
    return frappe.db.get_value("User", user, "full_name") or user


def _entry_has_field(fieldname):
    return bool(frappe.get_meta(ENTRY_DOCTYPE).has_field(fieldname))


def _entry_user_field_exists():
    return _entry_has_field("user")


# ---------------------------------------------------------------------------
# Device (the tablet's own login)
# ---------------------------------------------------------------------------

def _require_device():
    user = frappe.session.user
    if not user or user == "Guest":
        frappe.throw("This device is not signed in.", frappe.AuthenticationError)
    if not DEVICE_ALLOWED_ROLES.intersection(frappe.get_roles(user)):
        frappe.throw(
            f"This account is not set up as a kiosk device. Ask your admin to give it the '{DEVICE_ROLE}' role.",
            frappe.PermissionError,
        )
    return user


# ---------------------------------------------------------------------------
# Worker session (after a correct PIN)
# ---------------------------------------------------------------------------

def _token_key(token):
    return f"{CACHE_PREFIX}:worker_session:{token}"


def _session_ttl():
    # Server keeps the token a little longer than the tablet's on-screen
    # countdown, so the screen always times out first.
    return _settings().worker_session_seconds + 30


def _start_worker_session(worker, device, photo, method, detail=None):
    token = secrets.token_urlsafe(32)
    _cache().set_value(
        _token_key(token),
        {"user": worker, "device": device, "photo": photo, "method": method, "detail": detail},
        expires_in_sec=_session_ttl(),
    )
    return token


def _end_worker_session(token):
    if token:
        _cache().delete_value(_token_key(token))


def _worker(worker_token):
    """Resolves the worker behind a token, or throws SESSION_ENDED_MSG.

    The token must belong to THIS device's session (so a token copied to
    another browser is useless), and the worker must still be enabled.
    Each successful call slides the expiry forward.
    """
    device = _require_device()
    if not worker_token:
        frappe.throw(SESSION_ENDED_MSG, frappe.PermissionError)
    key = _token_key(worker_token)
    data = _cache().get_value(key, expires=True)
    if not data or data.get("device") != device:
        frappe.throw(SESSION_ENDED_MSG, frappe.PermissionError)
    if not frappe.db.get_value(WORKER_DOCTYPE, data["user"], "enabled"):
        _end_worker_session(worker_token)
        frappe.throw(SESSION_ENDED_MSG, frappe.PermissionError)
    _cache().set_value(key, data, expires_in_sec=_session_ttl())
    frappe.local.shared_kiosk_session = data
    return data["user"]


def _session():
    return getattr(frappe.local, "shared_kiosk_session", None) or {}


def _session_photo():
    return _session().get("photo")


# --- PIN lockout -------------------------------------------------------------

def _fail_key(user, kind="pin"):
    return f"{CACHE_PREFIX}:{kind}_fail:{user}"


def _failure_count(user, kind="pin"):
    return cint(_cache().get_value(_fail_key(user, kind), expires=True))


def _register_failure(user, kind="pin"):
    count = _failure_count(user, kind) + 1
    _cache().set_value(_fail_key(user, kind), count, expires_in_sec=_settings().lockout_minutes * 60)
    return count


def _clear_failures(user, kind="pin"):
    _cache().delete_value(_fail_key(user, kind))


def _locked_minutes(user, kind="pin"):
    """0 if the worker may try again, else minutes left on their lock."""
    s = _settings()
    limit = s.max_pin_attempts
    if _failure_count(user, kind) < limit:
        return 0
    c = _cache()
    try:
        secs = c.ttl(c.make_key(_fail_key(user, kind)))
    except Exception:
        secs = s.lockout_minutes * 60
    return max(1, int((secs if secs and secs > 0 else 60) + 59) // 60)


def _check_pin(user, pin):
    try:
        check_password(user, str(pin or ""), doctype=WORKER_DOCTYPE, fieldname=PIN_FIELD, delete_tracker_cache=False)
        return True
    except frappe.AuthenticationError:
        return False


# --- Photo + audit log -------------------------------------------------------

def _clean_photo(photo, max_chars=MAX_PHOTO_B64_CHARS):
    """Accepts a data:image/jpeg;base64,... string from the tablet camera.
    Returns the bare base64 string, or None if missing/invalid."""
    if not photo or not isinstance(photo, str):
        return None
    if "," in photo:
        photo = photo.split(",", 1)[1]
    if len(photo) > max_chars:
        return None
    try:
        raw = base64.b64decode(photo, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw.startswith(b"\xff\xd8\xff"):  # JPEG only
        return None
    return photo


def _save_work_photo(doc, user, work_photo, kind="stop"):
    """Attaches a back-camera photo ("start" or "stop") to the Employee
    Timesheet (supervisors see it under the timesheet's Attachments) and
    returns its link, which is also stored on the Timesheet Entry row.
    NOT wrapped in try/except on purpose: if the photo can't be saved, the
    Start / Stop fails and is rolled back, because the photo is compulsory."""
    stamp = now_datetime().strftime("%Y%m%d-%H%M%S")
    who = (user or "worker").split("@")[0].replace(".", "-")[:30]
    f = frappe.get_doc({
        "doctype": "File",
        "file_name": f"{doc.name}-{who}-{stamp}-{kind}.jpg",
        "attached_to_doctype": TS_DOCTYPE,
        "attached_to_name": doc.name,
        "is_private": 1,
        "content": base64.b64decode(work_photo),
    })
    f.flags.ignore_permissions = True
    f.insert()
    return f.file_url


def _timesheets_started_today(ts_names):
    """Timesheets that already have at least one entry dated today (by anyone)."""
    if not ts_names:
        return set()
    rows = frappe.db.sql(
        f"""select distinct parent from `tab{ENTRY_DOCTYPE}`
            where parenttype = %(pt)s and parent in %(names)s and entry_date = %(today)s""",
        {"pt": TS_DOCTYPE, "names": tuple(ts_names), "today": today()},
    )
    return {r[0] for r in rows}


def _start_photo_needed(doc):
    """True for the first Start of the day on this timesheet (WIH item)."""
    if doc.docstatus != 0:
        return False
    t = str(today())
    return not any(str(e.entry_date or "") == t for e in (doc.get("timesheet_entry") or []))


def _log(action, worker, doc=None, activity=None, details=None, photo=None, auth_method=None, work_photo=None):
    """Writes one Amal Timesheet Punch Log row (+ photo). Never allowed to break the
    actual punch: any failure here is recorded in Error Log and skipped."""
    try:
        log = frappe.get_doc({
            "doctype": LOG_DOCTYPE,
            "punch_time": now_datetime(),
            "action": action,
            "worker": worker,
            "worker_name": _full_name(worker),
            "timesheet": doc.name if doc else None,
            "wih_number": doc.get("wih_number") if doc else None,
            "item_code": doc.get(TS_ITEM_FIELD) if doc else None,
            "activity": activity,
            "device_user": frappe.session.user,
            "auth_method": auth_method or _session().get("method"),
            "details": " | ".join(x for x in [details, _session().get("detail") if not auth_method else None] if x) or None,
        })
        log.flags.ignore_permissions = True
        log.flags.ignore_links = True
        log.insert()

        if photo:
            f = frappe.get_doc({
                "doctype": "File",
                "file_name": f"{log.name}.jpg",
                "attached_to_doctype": LOG_DOCTYPE,
                "attached_to_name": log.name,
                "attached_to_field": "photo",
                "is_private": 1,
                "content": base64.b64decode(photo),
            })
            f.flags.ignore_permissions = True
            f.insert()
            log.db_set("photo", f.file_url, update_modified=False)

        if work_photo:
            wf = frappe.get_doc({
                "doctype": "File",
                "file_name": f"{log.name}-work-done.jpg",
                "attached_to_doctype": LOG_DOCTYPE,
                "attached_to_name": log.name,
                "attached_to_field": "work_photo",
                "is_private": 1,
                "content": base64.b64decode(work_photo),
            })
            wf.flags.ignore_permissions = True
            wf.insert()
            log.db_set("work_photo", wf.file_url, update_modified=False)
    except Exception:
        frappe.log_error(title="Amal Timesheet: could not write Amal Timesheet Punch Log")


# ---------------------------------------------------------------------------
# Timesheet helpers (ported from timesheet_kiosk v4 — the only change is that
# `user` is always passed in explicitly, because frappe.session.user is now
# the TABLET, not the worker)
# ---------------------------------------------------------------------------

def _allocation_child():
    """(child doctype, its User-link fieldname) behind Timesheet Allocation's
    Table MultiSelect `user` field — read from DocType metadata, not hardcoded."""
    meta = frappe.get_meta(ALLOCATION_DOCTYPE)
    user_field = meta.get_field("user")
    if not user_field or not user_field.options:
        frappe.throw(
            f"{ALLOCATION_DOCTYPE}.user is not configured as expected "
            "(expected a Table MultiSelect field with a child doctype)."
        )
    child_doctype = user_field.options
    link_fieldname = next(
        (f.fieldname for f in frappe.get_meta(child_doctype).fields if f.fieldtype in ("Link", "Dynamic Link")),
        None,
    )
    if not link_fieldname:
        frappe.throw(f"Could not find a Link field on {child_doctype} to match the user against.")
    return child_doctype, link_fieldname


def _norm(item):
    return (item or "").strip()


def _item_mode():
    """True when both Employee Timesheet.item_code and Timesheet Allocation.item
    exist. Without them the app falls back to one timesheet per WIH."""
    return bool(frappe.get_meta(TS_DOCTYPE).has_field(TS_ITEM_FIELD)
                and frappe.get_meta(ALLOCATION_DOCTYPE).has_field(ALLOC_ITEM_FIELD))


def _allocations(wihs=None, user=None):
    """[{wih, item, users}] — one per (WIH, item) from Timesheet Allocation.
    item is "" for older allocations made against the whole WIH."""
    filters = {"wih": ["in", list(wihs)]} if wihs else {"wih": ["is", "set"]}
    child_doctype, link_fieldname = _allocation_child()
    if user:
        mine = frappe.get_all(
            child_doctype,
            filters={"parenttype": ALLOCATION_DOCTYPE, "parentfield": "user", link_fieldname: user},
            pluck="parent", limit_page_length=0,
        )
        if not mine:
            return []
        filters["name"] = ["in", list(set(mine))]
    item_mode = _item_mode()
    fields = ["name", "wih"] + ([ALLOC_ITEM_FIELD] if item_mode else [])
    allocs = frappe.get_all(ALLOCATION_DOCTYPE, filters=filters, fields=fields,
                            order_by="creation asc", limit_page_length=0)
    if not allocs:
        return []
    rows = frappe.get_all(
        child_doctype,
        filters={"parenttype": ALLOCATION_DOCTYPE, "parentfield": "user", "parent": ["in", [a.name for a in allocs]]},
        fields=["parent", link_fieldname + " as user", "idx"],
        order_by="idx asc", limit_page_length=0,
    )
    by_alloc = {}
    for r in rows:
        if r.user:
            by_alloc.setdefault(r.parent, []).append(r.user)
    merged = {}
    for a in allocs:
        key = (a.wih, _norm(a.get(ALLOC_ITEM_FIELD)) if item_mode else "")
        entry = merged.setdefault(key, {"wih": key[0], "item": key[1], "users": []})
        for u in by_alloc.get(a.name, []):
            if u not in entry["users"]:
                entry["users"].append(u)
    return list(merged.values())


def _allocated_pairs(user):
    """{(wih, item)} this user is allocated to."""
    return {(a["wih"], a["item"]) for a in _allocations(user=user)}


def _allocated_wih_numbers(user):
    return list(dict.fromkeys(w for w, _ in sorted(_allocated_pairs(user))))


def _doc_pair(doc):
    return (doc.get("wih_number"), _norm(doc.get(TS_ITEM_FIELD)) if _item_mode() else "")


def _is_allocated_to_doc(doc, user):
    if not doc.get("wih_number"):
        return False
    return _doc_pair(doc) in _allocated_pairs(user)


def _find_timesheet(wih, item):
    """The Draft/Submitted Employee Timesheet for one (WIH, item), or None."""
    filters = {"wih_number": wih, "docstatus": ["in", [0, 1]]}
    if _item_mode():
        filters[TS_ITEM_FIELD] = item if item else ["is", "not set"]
    rows = frappe.get_all(TS_DOCTYPE, filters=filters, pluck="name", order_by="creation asc", limit_page_length=1)
    return rows[0] if rows else None


def _timesheets_for_wihs(wihs):
    """{(wih, item): row} for Draft/Submitted timesheets of these WIHs."""
    if not wihs:
        return {}
    item_mode = _item_mode()
    fields = ["name", "wih_number", "docstatus", "total_hours", "product_name"] + ([TS_ITEM_FIELD] if item_mode else [])
    rows = frappe.get_all(TS_DOCTYPE, filters={"wih_number": ["in", list(wihs)], "docstatus": ["in", [0, 1]]},
                          fields=fields, order_by="creation desc", limit_page_length=0)
    out = {}
    for r in rows:  # oldest wins, matching _find_timesheet
        out[(r.wih_number, _norm(r.get(TS_ITEM_FIELD)) if item_mode else "")] = r
    return out


def _scope_sql(user, values, alias="ts"):
    """SQL condition: timesheets this user may see in their own lists —
    ones they created, or ones for a (WIH, item) they're allocated to."""
    values["user"] = user
    conds = [f"{alias}.user = %(user)s"]
    pairs = sorted(_allocated_pairs(user))
    if pairs:
        if _item_mode():
            for i, (w, it) in enumerate(pairs):
                values[f"aw{i}"], values[f"ai{i}"] = w, it
                conds.append(f"({alias}.wih_number = %(aw{i})s and ifnull({alias}.{TS_ITEM_FIELD}, '') = %(ai{i})s)")
        else:
            values["allocated"] = tuple(w for w, _ in pairs)
            conds.append(f"{alias}.wih_number in %(allocated)s")
    return "(" + " or ".join(conds) + ")"


def _is_mine(entry, user):
    """Row belongs to `user` — or has no owner recorded (legacy rows)."""
    owner = entry.get("user")
    return not owner or owner == user


def _check_access(doc, user):
    """Worker may open a shared WIH timesheet if they created it, are
    allocated to its WIH, or have personally punched an entry on it."""
    if doc.get("user") == user:
        return
    if _is_allocated_to_doc(doc, user):
        return
    if any(e.get("user") == user for e in (doc.get("timesheet_entry") or [])):
        return
    frappe.throw("You are not permitted to access this timesheet.", frappe.PermissionError)


def _is_timesheet_manager(user):
    return TIMESHEET_MANAGER_ROLE in frappe.get_roles(user)


def _check_manual_allowed(user):
    if not _settings().allow_manual_entries and not _is_timesheet_manager(user):
        frappe.throw(
            "Adding, editing or deleting entries on the kiosk is limited to Timesheet Managers.",
            frappe.PermissionError,
        )


def _running_timer_users(doc):
    running_users = list(dict.fromkeys(
        e.get("user") for e in (doc.get("timesheet_entry") or [])
        if e.get("is_running") and e.get("user")
    ))
    if not running_users:
        return []
    users = frappe.get_all("User", filters={"name": ["in", running_users]}, fields=["name", "full_name"])
    name_map = {u.name: (u.full_name or u.name) for u in users}
    return [{"user": u, "full_name": name_map.get(u, u)} for u in running_users]


def _recalc_total_hours(doc):
    doc.total_hours = round(sum(flt(e.duration_hours) for e in doc.timesheet_entry), 2)


def _status_label(docstatus):
    return {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(docstatus, "Draft")


def _build_timesheet_response(doc, user):
    entries = doc.get("timesheet_entry") or []
    my_entries = [e for e in entries if _is_mine(e, user)]

    active_entry = next(
        (e for e in my_entries if e.get("is_running") or (e.start_time and not e.end_time)),
        None,
    )
    active_timer_started_at = None
    active_timer_elapsed_hours = None
    if active_entry:
        active_timer_started_at = str(active_entry.start_time).replace(" ", "T")
        active_timer_elapsed_hours = round(time_diff_in_hours(now_datetime(), active_entry.start_time), 4)

    entry_list = []
    for e in sorted(my_entries, key=lambda row: row.idx):
        entry_list.append({
            "name": e.name,
            "idx": e.idx,
            "entry_date": str(e.entry_date) if e.entry_date else None,
            "start_time": str(e.start_time) if e.start_time else None,
            "end_time": str(e.end_time) if e.end_time else None,
            "duration_hours": e.duration_hours,
            "minutes": e.get("minutes"),
            "activity": e.get("activity"),
            "notes": e.notes,
            "is_running": bool(e.get("is_running")),
        })

    personal_hours = round(sum(flt(e.duration_hours) for e in my_entries if not e.get("is_running")), 2)

    wih_photo = None
    if doc.get("wih_number"):
        wih_photo = frappe.db.get_value("Work In Hand", doc.wih_number, "photos")

    is_manager = _is_timesheet_manager(user)
    running_timer_users = _running_timer_users(doc) if (is_manager and doc.docstatus == 0) else []

    return {
        "name": doc.name,
        "wih_number": doc.get("wih_number"),
        "item_code": doc.get(TS_ITEM_FIELD),
        "wih_photo": wih_photo,
        "employee": doc.get("user") or "",
        "employee_name": doc.get("user"),
        "product_name": doc.get("product_name"),
        "start_date": str(doc.start_date) if doc.get("start_date") else None,
        "end_date": str(doc.end_date) if doc.get("end_date") else None,
        "status": _status_label(doc.docstatus),
        "docstatus": doc.docstatus,
        "total_hours": doc.get("total_hours"),
        "personal_hours": personal_hours,
        "final_hrs": doc.get("final_hrs"),
        "notes": doc.get("notes"),
        "timesheet_entry": entry_list,
        "start_photo_needed": _start_photo_needed(doc),
        "active_timer_started_at": active_timer_started_at,
        "active_timer_elapsed_hours": active_timer_elapsed_hours,
        "is_timesheet_manager": is_manager,
        "can_edit_entries": bool(_settings().allow_manual_entries or is_manager),
        "running_timer_users": running_timer_users,
    }


def _running_entries_for_users(users):
    """{user: [{timesheet, wih_number, activity, elapsed_hours}, ...]} for
    every currently running entry of these workers on a Draft timesheet."""
    if not users or not _entry_user_field_exists():
        return {}
    activity_col = "te.activity" if _entry_has_field("activity") else "null"
    item_col = f"et.{TS_ITEM_FIELD}" if _item_mode() else "null"
    rows = frappe.db.sql(
        f"""
        select te.user, te.parent as timesheet, et.wih_number, te.start_time,
               {activity_col} as activity, {item_col} as item_code
        from `tab{ENTRY_DOCTYPE}` te
        inner join `tab{TS_DOCTYPE}` et on et.name = te.parent
        where te.parenttype = %(pt)s
          and te.is_running = 1
          and et.docstatus = 0
          and te.user in %(users)s
        order by te.start_time asc
        """,
        {"pt": TS_DOCTYPE, "users": tuple(users)},
        as_dict=True,
    )
    now = now_datetime()
    out = {}
    for r in rows:
        out.setdefault(r.user, []).append({
            "timesheet": r.timesheet,
            "wih_number": r.wih_number,
            "item_code": r.item_code,
            "activity": r.activity,
            "elapsed_hours": round(time_diff_in_hours(now, r.start_time), 4) if r.start_time else 0,
        })
    return out


def _get_doc_for_worker(name, user):
    doc = frappe.get_doc(TS_DOCTYPE, name)
    # Core permissions don't know about the WIH-sharing model (and the
    # session user is the tablet anyway) — _check_access is the real gate.
    doc.flags.ignore_permissions = True
    _check_access(doc, user)
    return doc


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def healthz():
    return {"status": "ok", "app": "amal_timesheet"}


# ---------------------------------------------------------------------------
# Device sign-in
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def reset_session():
    """GET before device_login(): clears any older session cookie on this
    browser (e.g. someone's ERPNext desk login) so the login POST isn't
    rejected as a CSRF mismatch."""
    if frappe.session.user != "Guest":
        frappe.local.login_manager.logout()
        frappe.db.commit()
    return {"ok": True}


@frappe.whitelist(allow_guest=True)
def device_login(usr=None, pwd=None):
    if not usr or not pwd:
        frappe.throw("Username and password are required.")
    login_manager = LoginManager()
    login_manager.authenticate(user=usr, pwd=pwd)
    login_manager.post_login()
    user = login_manager.user

    if not DEVICE_ALLOWED_ROLES.intersection(frappe.get_roles(user)):
        login_manager.logout()
        frappe.db.commit()
        frappe.throw(
            f"This account can't run the kiosk. Ask your admin to give it the '{DEVICE_ROLE}' role.",
            frappe.PermissionError,
        )
    frappe.db.commit()
    return {
        "device_user": user,
        "device_name": _full_name(user),
        "csrf_token": frappe.sessions.get_csrf_token(),
        "settings": _public_settings(),
    }


@frappe.whitelist()
def get_device_info():
    device = _require_device()
    return {"device_user": device, "device_name": _full_name(device), "settings": _public_settings()}


@frappe.whitelist()
def device_logout(worker_token=None):
    """Signing the TABLET out needs a Timesheet Manager's worker session, so
    a random person at the kiosk can't lock everyone out."""
    user = _worker(worker_token)
    if not _is_timesheet_manager(user):
        frappe.throw("Only a Timesheet Manager can sign this device out.", frappe.PermissionError)
    _end_worker_session(worker_token)
    frappe.local.login_manager.logout()
    frappe.db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Worker tiles + PIN
# ---------------------------------------------------------------------------

@frappe.whitelist()
def list_workers():
    _require_device()
    workers = frappe.get_all(
        WORKER_DOCTYPE,
        filters={"enabled": 1},
        fields=["name", "user", "full_name", "photo", "has_pin"],
        order_by="full_name asc",
        limit_page_length=0,
    )
    running = _running_entries_for_users([w.user for w in workers])
    return [
        {
            "user": w.user,
            "full_name": w.full_name or w.user,
            "photo": w.photo,
            "has_pin": bool(w.has_pin),
            "locked_minutes": _locked_minutes(w.user),
            "running": running.get(w.user, []),
        }
        for w in workers
    ]


@frappe.whitelist()
def verify_pin(user, pin, photo=None):
    device = _require_device()
    s = _settings()

    worker = frappe.db.get_value(
        WORKER_DOCTYPE, user, ["name", "user", "full_name", "enabled", "has_pin"], as_dict=True
    )
    if not worker or not worker.enabled:
        frappe.throw("Worker not found or disabled.")
    if not worker.has_pin:
        frappe.throw("No PIN has been set for you yet. Please ask your supervisor to set one.")

    photo = _clean_photo(photo)
    if s.require_photo and not photo:
        frappe.throw("A photo is required to sign in. Please allow the camera and look at the screen.")

    locked = _locked_minutes(worker.name)
    if locked:
        frappe.throw(f"Too many wrong PINs. Please try again in {locked} min or ask your supervisor.")

    if not _check_pin(worker.name, pin):
        count = _register_failure(worker.name)
        _log("PIN Failed", worker.user, details=f"Wrong PIN attempt {count}", photo=photo, auth_method="PIN")
        frappe.db.commit()
        left = s.max_pin_attempts - count
        if left <= 0:
            frappe.throw(f"Too many wrong PINs. Locked for {s.lockout_minutes} minutes.")
        frappe.throw(f"Wrong PIN. {left} attempt{'s' if left != 1 else ''} left.")

    _clear_failures(worker.name)
    token = _start_worker_session(worker.user, device, photo, "PIN")
    return {
        "worker_token": token,
        "user": worker.user,
        "full_name": worker.full_name or worker.user,
        "is_timesheet_manager": _is_timesheet_manager(worker.user),
        "running": _running_entries_for_users([worker.user]).get(worker.user, []),
        "session_seconds": s.worker_session_seconds,
    }


@frappe.whitelist()
def end_worker_session(worker_token=None):
    _end_worker_session(worker_token)
    return {"ok": True}


@frappe.whitelist()
def ping_worker_session(worker_token=None):
    """Keeps a worker's session alive while they are actively touching the
    screen (scrolling entries etc.) without making any other API call."""
    _worker(worker_token)
    return {"ok": True}


@frappe.whitelist()
def change_pin(worker_token=None, old_pin=None, new_pin=None):
    user = _worker(worker_token)
    if not _check_pin(user, old_pin):
        count = _register_failure(user)
        frappe.db.commit()
        if count >= _settings().max_pin_attempts:
            _end_worker_session(worker_token)
            frappe.throw(SESSION_ENDED_MSG, frappe.PermissionError)
        frappe.throw("Current PIN is wrong.")
    new_pin = validate_pin_format(new_pin)
    if str(old_pin).strip() == new_pin:
        frappe.throw("New PIN must be different from the current PIN.")
    _clear_failures(user)
    update_password(user, new_pin, doctype=WORKER_DOCTYPE, fieldname=PIN_FIELD)
    frappe.db.set_value(WORKER_DOCTYPE, user, {"has_pin": 1, "pin_last_changed": now_datetime()})
    _log("PIN Changed", user, photo=_session_photo())
    frappe.db.commit()
    return {"ok": True}



# ---------------------------------------------------------------------------
# Timesheet-first screens (what the tablet shows before anyone verifies)
#
# The tablet lists every WIH that has a Timesheet Allocation. Opening one
# shows the people allocated to it, each with their own Start / Stop button.
# These calls READ names and timer status; Start / Stop from this screen is a
# plain tap (tap_start / tap_stop below), with the compulsory photos.
# ---------------------------------------------------------------------------

@frappe.whitelist()
def list_kiosk_timesheets(search=None):
    _require_device()
    allocs = _allocations()
    if not allocs:
        return []
    by_wih = {}
    for a in allocs:
        by_wih.setdefault(a["wih"], []).append(a)
    wihs = list(by_wih)
    sheets = _timesheets_for_wihs(wihs)
    codes = {r.name: r.client_code for r in frappe.get_all(
        "Work In Hand", filters={"name": ["in", wihs]}, fields=["name", "client_code"], limit_page_length=0)}

    running_users = {}
    draft = [t.name for t in sheets.values() if t.docstatus == 0]
    if draft and _entry_user_field_exists():
        ts_wih = {t.name: t.wih_number for t in sheets.values()}
        for r in frappe.db.sql(
            f"""select distinct parent, user from `tab{ENTRY_DOCTYPE}`
                where parenttype = %(pt)s and parent in %(names)s and is_running = 1""",
            {"pt": TS_DOCTYPE, "names": tuple(draft)}, as_dict=True,
        ):
            running_users.setdefault(ts_wih[r.parent], set()).add(r.user)

    out = []
    q = (search or "").strip().lower()
    for wih, wa in by_wih.items():
        pairs = [(wih, a["item"]) for a in wa]
        # A WIH stays on the list while at least one allocated item is still open.
        if all(sheets.get(p) and sheets[p].docstatus == 1 for p in pairs):
            continue
        any_ts = next((sheets[p] for p in pairs if sheets.get(p)), None)
        style = codes.get(wih) or (any_ts.product_name if any_ts else None)
        items = [a["item"] for a in wa if a["item"]]
        if q and q not in wih.lower() and q not in (style or "").lower() and not any(q in i.lower() for i in items):
            continue
        team = []
        for a in wa:
            team += [u for u in a["users"] if u not in team]
        out.append({
            "wih_number": wih,
            "style": style,
            "items": items,
            "timesheet": any_ts.name if any_ts else None,
            "team_size": len(team),
            "working_now": len(running_users.get(wih, set())),
            "team_hours": round(sum(flt(t.total_hours) for (w, _), t in sheets.items() if w == wih), 4),
        })
    out.sort(key=lambda r: (-r["working_now"], r["wih_number"]))
    return out


@frappe.whitelist()
def get_kiosk_timesheet(wih=None):
    _require_device()
    if not wih:
        frappe.throw("WIH not specified.")
    allocs = _allocations([wih])
    sheets = _timesheets_for_wihs([wih])
    if not allocs and not sheets:
        frappe.throw("Nobody is allocated to this WIH. Add people in Timesheet Allocation.")
    wih_row = frappe.db.get_value("Work In Hand", wih, ["client_code", "photos"], as_dict=True) or frappe._dict()
    ts_by_name = {t.name: (item, t) for (_, item), t in sheets.items()}

    users, allocated_items = [], {}
    for a in allocs:
        for u in a["users"]:
            if u not in users:
                users.append(u)
            allocated_items.setdefault(u, []).append(a["item"])

    # Hours each person logged on this WIH's timesheets, and what's running.
    hours, running_here = {}, {}
    if ts_by_name and _entry_user_field_exists():
        activity_col = "activity" if _entry_has_field("activity") else "null"
        for r in frappe.db.sql(
            f"""select parent, user, is_running, duration_hours, start_time, {activity_col} as activity
                from `tab{ENTRY_DOCTYPE}` where parenttype = %(pt)s and parent in %(names)s""",
            {"pt": TS_DOCTYPE, "names": tuple(ts_by_name)}, as_dict=True,
        ):
            item, t = ts_by_name[r.parent]
            if r.is_running and t.docstatus == 0:
                running_here[r.user] = {
                    "timesheet": r.parent,
                    "item_code": item,
                    "activity": r.activity,
                    "elapsed_hours": round(time_diff_in_hours(now_datetime(), r.start_time), 4) if r.start_time else 0,
                }
            elif not r.is_running:
                hours[r.user] = hours.get(r.user, 0) + flt(r.duration_hours)

    allocated = set(users)
    # People who logged time here but were later removed from the allocation
    # still appear (at the end), so a running timer can always be stopped.
    users = list(dict.fromkeys(users + [u for u in list(running_here) + list(hours) if u and u not in allocated]))
    names = {u.name: u.full_name for u in frappe.get_all(
        "User", filters={"name": ["in", users or [""]]}, fields=["name", "full_name"], limit_page_length=0)}
    workers = {w.name: w for w in frappe.get_all(
        WORKER_DOCTYPE, filters={"name": ["in", users or [""]]},
        fields=["name", "enabled", "photo", "has_pin"], limit_page_length=0)}
    running_any = _running_entries_for_users(users)
    started_today = _timesheets_started_today(list(ts_by_name))

    people = []
    for u in users:
        w = workers.get(u)
        elsewhere = [r for r in running_any.get(u, []) if r["timesheet"] not in ts_by_name]
        items = []
        for it in allocated_items.get(u, []):
            t = sheets.get((wih, it))
            items.append({"item_code": it, "timesheet": t.name if t else None,
                          "submitted": bool(t and t.docstatus == 1),
                          "start_photo_needed": not (t and t.name in started_today)})
        people.append({
            "user": u,
            "full_name": names.get(u) or u,
            "allocated": u in allocated,
            "items": items,
            "photo": w.photo if w else None,
            # Anyone allocated can tap Start / Stop. Only an Amal Timesheet
            # Worker record with Enabled unticked takes a person off the tablet.
            "on_tablet": not (w and not w.enabled),
            "has_pin": bool(w and w.has_pin),
            "running": running_here.get(u),
            "running_elsewhere": elsewhere[0]["wih_number"] if elsewhere else None,
            "hours": round(hours.get(u, 0), 4),
        })

    item_list = []
    for a in allocs:
        t = sheets.get((wih, a["item"]))
        item_list.append({"item_code": a["item"], "timesheet": t.name if t else None,
                          "docstatus": t.docstatus if t else 0, "hours": flt(t.total_hours) if t else 0,
                          "start_photo_needed": not (t and t.name in started_today)})
    any_ts = next(iter(sheets.values()), None)
    return {
        "wih_number": wih,
        "style": wih_row.client_code or (any_ts.product_name if any_ts else None),
        "wih_photo": wih_row.photos,
        "items": item_list,
        "all_submitted": bool(item_list) and all(i["docstatus"] == 1 for i in item_list),
        "team_hours": round(sum(flt(t.total_hours) for t in sheets.values()), 4),
        "people": people,
    }

# ---------------------------------------------------------------------------
# Work In Hand (WIH) — from the worker's Timesheet Allocation
# ---------------------------------------------------------------------------

@frappe.whitelist()
def list_wih(worker_token=None, search=None):
    user = _worker(worker_token)
    pairs = sorted(_allocated_pairs(user))
    sheets = _timesheets_for_wihs({w for w, _ in pairs})
    pairs = [p for p in pairs if p not in sheets]
    if search:
        q = search.lower()
        pairs = [p for p in pairs if q in p[0].lower() or q in p[1].lower()]
    codes = {}
    if pairs:
        codes = {r.name: r.client_code for r in frappe.get_all(
            "Work In Hand", filters={"name": ["in", list({w for w, _ in pairs})]}, fields=["name", "client_code"])}
    return [
        {"name": w, "item_code": it, "client_code": codes.get(w), "product_name": None, "status": None}
        for w, it in pairs
    ]


@frappe.whitelist()
def get_activity_options():
    _require_device()
    field = frappe.get_meta(ENTRY_DOCTYPE).get_field("activity")
    if not field or not field.options:
        return []
    return [o.strip() for o in field.options.split("\n") if o.strip()]


# ---------------------------------------------------------------------------
# Timesheets
# ---------------------------------------------------------------------------

def _running_timesheet_names(ts_names, user):
    if not ts_names or not _entry_user_field_exists():
        return set()
    rows = frappe.db.sql(
        f"""
        select distinct te.parent
        from `tab{ENTRY_DOCTYPE}` te
        where te.parenttype = %(pt)s
          and te.parent in %(names)s
          and te.is_running = 1
          and (te.user = %(user)s or te.user is null or te.user = '')
        """,
        {"pt": TS_DOCTYPE, "names": tuple(ts_names), "user": user},
    )
    return {r[0] for r in rows}


@frappe.whitelist()
def list_timesheets(worker_token=None, status=None, wih_number=None, page=1):
    user = _worker(worker_token)
    page = max(1, cint(page) or 1)
    start = (page - 1) * PAGE_SIZE

    values = {}
    scope_clause = _scope_sql(user, values)

    if status == "Draft":
        status_clause = "and ts.docstatus = 0"
    elif status == "Submitted":
        status_clause = "and ts.docstatus = 1"
    else:
        status_clause = "and ts.docstatus != 2"

    search_clause = ""
    item_col = f"ts.{TS_ITEM_FIELD}" if _item_mode() else "null"
    if wih_number:
        search_clause = (f"and (ts.wih_number like %(search)s or ts.{TS_ITEM_FIELD} like %(search)s)"
                         if _item_mode() else "and ts.wih_number like %(search)s")
        values["search"] = f"%{wih_number}%"

    total = frappe.db.sql(
        f"select count(*) from `tab{TS_DOCTYPE}` ts where {scope_clause} {status_clause} {search_clause}",
        values,
    )[0][0]

    items = frappe.db.sql(
        f"""
        select ts.name, ts.wih_number, ts.product_name, ts.start_date, ts.end_date,
               ts.docstatus, ts.total_hours, ts.notes, {item_col} as item_code
        from `tab{TS_DOCTYPE}` ts
        where {scope_clause} {status_clause} {search_clause}
        order by ts.modified desc
        limit %(page_size)s offset %(start)s
        """,
        {**values, "page_size": PAGE_SIZE, "start": start},
        as_dict=True,
    )

    running_names = _running_timesheet_names([i["name"] for i in items], user)
    for item in items:
        item["is_running_for_me"] = item["name"] in running_names
        item["status"] = _status_label(item["docstatus"])

    return {"items": items, "total": total, "page": page, "page_size": PAGE_SIZE}


@frappe.whitelist()
def create_timesheet(worker_token=None, wih_number=None, product_name=None, start_date=None, end_date=None, item_code=None):
    """Opens (or creates) the timesheet for one (WIH, item). Safe to call
    repeatedly: everyone on the same item shares one timesheet."""
    user = _worker(worker_token)
    doc = _open_timesheet(user, wih_number, item_code, product_name, start_date, end_date)
    frappe.db.commit()
    return _build_timesheet_response(doc, user)


def _open_timesheet(user, wih_number, item_code=None, product_name=None, start_date=None, end_date=None):
    """Returns the Employee Timesheet for one (WIH, item), creating it the
    first time. Everyone on the same item shares one timesheet."""
    if not wih_number:
        frappe.throw("Please select a WIH order.")
    item = _norm(item_code)
    pairs = _allocated_pairs(user)
    if _item_mode() and not item:
        mine = sorted(it for w, it in pairs if w == wih_number)
        if len(mine) == 1:
            item = mine[0]
        elif len(mine) > 1:
            frappe.throw("You are allocated to more than one item on this WIH. Please choose the item.")

    existing = _find_timesheet(wih_number, item)
    if existing:
        return _get_doc_for_worker(existing, user)

    if (wih_number, item) not in pairs:
        frappe.throw("This item is not allocated to you." if item else "This WIH is not allocated to you.",
                     frappe.PermissionError)

    doc = frappe.new_doc(TS_DOCTYPE)
    doc.flags.ignore_permissions = True
    doc.wih_number = wih_number
    if _item_mode():
        doc.set(TS_ITEM_FIELD, item or None)
    doc.user = user
    doc.product_name = product_name
    doc.start_date = start_date or today()
    doc.end_date = end_date
    doc.status = "Draft"
    doc.total_hours = 0
    doc.insert()
    return doc


@frappe.whitelist()
def get_timesheet(worker_token=None, name=None):
    user = _worker(worker_token)
    doc = _get_doc_for_worker(name, user)
    if doc.docstatus == 2:
        frappe.throw("This timesheet has been cancelled.", frappe.DoesNotExistError)
    return _build_timesheet_response(doc, user)


@frappe.whitelist()
def update_timesheet(worker_token=None, name=None, product_name=None, start_date=None, end_date=None, notes=None):
    user = _worker(worker_token)
    doc = _get_doc_for_worker(name, user)
    if product_name is not None:
        doc.product_name = product_name
    if start_date is not None:
        doc.start_date = start_date
    if end_date is not None:
        doc.end_date = end_date
    if notes is not None:
        doc.notes = notes
    doc.save()
    frappe.db.commit()
    return _build_timesheet_response(doc, user)


@frappe.whitelist()
def submit_timesheet(worker_token=None, name=None, final_hrs=None):
    user = _worker(worker_token)
    doc = _get_doc_for_worker(name, user)

    if not _is_timesheet_manager(user):
        frappe.throw(
            "Only a Timesheet Manager can submit this timesheet. You can still add or edit your own time entries.",
            frappe.PermissionError,
        )

    running = _running_timer_users(doc)
    if running or any(e.get("is_running") for e in doc.timesheet_entry):
        names = ", ".join(r["full_name"] for r in running) or "a teammate"
        frappe.throw(f"Cannot submit — timer still running for: {names}. Ask them to stop it first.")

    final_hrs = flt(final_hrs)
    if final_hrs <= 0:
        frappe.throw("Enter the final hours (as confirmed by the supervisor) before submitting.")

    doc.final_hrs = final_hrs
    doc.status = "Submitted"
    doc.save()
    doc.submit()

    if doc.get("wih_number"):
        frappe.db.set_value("Work In Hand", doc.wih_number, "final_hrs", final_hrs)

    _log("Submit", user, doc=doc, details=f"Final hrs: {final_hrs}", photo=_session_photo())
    frappe.db.commit()
    return _build_timesheet_response(doc, user)


# --- Timer -------------------------------------------------------------------

def _do_start(user, doc, activity, notes=None, start_photo=None, auth_method=None):
    if doc.docstatus != 0:
        frappe.throw("This timesheet is already submitted.")
    if doc.get("wih_number") and not _is_allocated_to_doc(doc, user):
        frappe.throw("You are not allocated to this item any more, so you can't start a timer on it. "
                     "Ask your supervisor to check Timesheet Allocation.", frappe.PermissionError)
    if any(e.get("is_running") and _is_mine(e, user) for e in doc.timesheet_entry):
        frappe.throw("A timer is already running. Stop it before starting a new one.")

    if _settings().one_timer_per_worker:
        elsewhere = [
            r for r in _running_entries_for_users([user]).get(user, []) if r["timesheet"] != doc.name
        ]
        if elsewhere:
            frappe.throw(
                f"{_full_name(user)} already has a timer running on {elsewhere[0]['wih_number'] or elsewhere[0]['timesheet']}"
                f"{' / ' + elsewhere[0]['item_code'] if elsewhere[0].get('item_code') else ''}. "
                "Stop that one first."
            )

    activity = (activity or "").strip()
    if not activity:
        frappe.throw("Please select an activity before starting the timer.")

    # Compulsory start photo for the first entry of the day on this item.
    needed = _start_photo_needed(doc)
    start_photo = _clean_photo(start_photo, MAX_WORK_PHOTO_B64_CHARS)
    if needed and not start_photo:
        frappe.throw(START_PHOTO_REQUIRED_MSG)
    if not needed:
        start_photo = None  # later Starts that day keep no photo

    now = now_datetime()
    row = {
        "entry_date": now.date(),
        "start_time": now,
        "end_time": None,
        "duration_hours": 0,
        "minutes": 0,
        "activity": activity,
        "notes": notes,
        "is_running": 1,
        "user": user,
    }
    if start_photo:
        url = _save_work_photo(doc, user, start_photo, "start")
        if _entry_has_field(ENTRY_START_PHOTO_FIELD):
            row[ENTRY_START_PHOTO_FIELD] = url
    doc.append("timesheet_entry", row)
    _recalc_total_hours(doc)
    doc.save()
    _log("Start", user, doc=doc, activity=activity, photo=_session_photo(), auth_method=auth_method,
         work_photo=start_photo, details="First start of the day - start photo taken" if start_photo else None)
    return doc


def _do_stop(user, doc, notes=None, work_photo=None, auth_method=None):
    active = next((e for e in doc.timesheet_entry if e.get("is_running") and _is_mine(e, user)), None)
    if not active:
        frappe.throw("No active timer found for this person on this timesheet.")

    # Compulsory: no photo of the work done → the timer keeps running.
    work_photo = _clean_photo(work_photo, MAX_WORK_PHOTO_B64_CHARS)
    if not work_photo:
        frappe.throw(WORK_PHOTO_REQUIRED_MSG)

    now = now_datetime()
    duration = round(time_diff_in_hours(now, active.start_time), 4)
    active.end_time = now
    active.duration_hours = duration
    active.minutes = round(duration * 60, 2)
    active.is_running = 0
    active.user = user
    if notes is not None:
        active.notes = notes

    url = _save_work_photo(doc, user, work_photo, "stop")
    if _entry_has_field(ENTRY_PHOTO_FIELD):
        active.set(ENTRY_PHOTO_FIELD, url)

    _recalc_total_hours(doc)
    doc.save()
    _log("Stop", user, doc=doc, activity=active.get("activity"), auth_method=auth_method,
         details=f"Duration: {round(duration * 60)} min", photo=_session_photo(), work_photo=work_photo)
    return doc, duration


def _tap_user(user):
    """The person whose button was tapped on the WIH team screen. No PIN /
    face: anyone allocated can be started or stopped, unless their Amal
    Timesheet Worker record has Enabled unticked."""
    if not user or not frappe.db.exists("User", user):
        frappe.throw("Person not found.")
    if frappe.db.exists(WORKER_DOCTYPE, user) and not frappe.db.get_value(WORKER_DOCTYPE, user, "enabled"):
        frappe.throw(f"{_full_name(user)} is switched off in Amal Timesheet Worker. Ask your supervisor.")
    return user


@frappe.whitelist()
def tap_start(user=None, wih_number=None, item_code=None, activity=None, start_photo=None,
              product_name=None, start_date=None):
    """WIH team screen: plain tap Start for `user` on (WIH, item)."""
    _require_device()
    user = _tap_user(user)
    doc = _open_timesheet(user, wih_number, item_code, product_name, start_date)
    doc = _do_start(user, doc, activity, start_photo=start_photo, auth_method="Tap")
    frappe.db.commit()
    return {"wih_number": doc.get("wih_number"), "item_code": doc.get(TS_ITEM_FIELD), "timesheet": doc.name,
            "worker_name": _full_name(user), "activity": (activity or "").strip()}


@frappe.whitelist()
def tap_stop(user=None, timesheet=None, work_photo=None):
    """WIH team screen: plain tap Stop for `user` (the photo is compulsory)."""
    _require_device()
    user = _tap_user(user)
    doc = _get_doc_for_worker(timesheet, user)
    doc, duration = _do_stop(user, doc, work_photo=work_photo, auth_method="Tap")
    frappe.db.commit()
    return {"wih_number": doc.get("wih_number"), "item_code": doc.get(TS_ITEM_FIELD), "timesheet": doc.name,
            "worker_name": _full_name(user), "stopped_hours": duration}


@frappe.whitelist()
def start_timer(worker_token=None, name=None, activity=None, notes=None, start_photo=None):
    """My Hours timer screen (after the worker's PIN)."""
    user = _worker(worker_token)
    doc = _get_doc_for_worker(name, user)
    _do_start(user, doc, activity, notes=notes, start_photo=start_photo)

    # One PIN = one punch. The next person must enter their own PIN.
    _end_worker_session(worker_token)
    frappe.db.commit()

    resp = _build_timesheet_response(doc, user)
    resp.update({"session_ended": True, "worker_name": _full_name(user), "activity": activity})
    return resp


@frappe.whitelist()
def stop_timer(worker_token=None, name=None, notes=None, work_photo=None):
    """My Hours timer screen (after the worker's PIN)."""
    user = _worker(worker_token)
    doc = _get_doc_for_worker(name, user)
    doc, duration = _do_stop(user, doc, notes=notes, work_photo=work_photo)

    _end_worker_session(worker_token)
    frappe.db.commit()

    resp = _build_timesheet_response(doc, user)
    resp.update({"session_ended": True, "worker_name": _full_name(user), "stopped_hours": duration})
    return resp


# --- Manual entries ------------------------------------------------------------

@frappe.whitelist()
def add_entry(worker_token=None, name=None, entry_date=None, start_time=None, end_time=None,
              duration_hours=None, notes=None):
    user = _worker(worker_token)
    _check_manual_allowed(user)
    doc = _get_doc_for_worker(name, user)
    if doc.docstatus != 0:
        frappe.throw("Cannot add entries to a submitted timesheet.")
    if not (entry_date and start_time and end_time):
        frappe.throw("Please fill date, start and end time.")

    full_start = f"{entry_date} {start_time}"
    full_end = f"{entry_date} {end_time}"
    if duration_hours is None:
        duration_hours = round(time_diff_in_hours(full_end, full_start), 4)
    if flt(duration_hours) <= 0:
        frappe.throw("End time must be after start time.")

    doc.append("timesheet_entry", {
        "entry_date": entry_date,
        "start_time": full_start,
        "end_time": full_end,
        "duration_hours": duration_hours,
        "minutes": round(float(duration_hours) * 60, 2),
        "notes": notes or "Manual entry",
        "is_running": 0,
        "user": user,
    })
    _recalc_total_hours(doc)
    doc.save()
    _log("Manual Entry", user, doc=doc, details=f"{full_start} to {full_end}", photo=_session_photo())
    frappe.db.commit()
    return _build_timesheet_response(doc, user)


@frappe.whitelist()
def update_entry(worker_token=None, name=None, idx=None, entry_date=None, start_time=None, end_time=None,
                 duration_hours=None, notes=None):
    user = _worker(worker_token)
    _check_manual_allowed(user)
    doc = _get_doc_for_worker(name, user)
    if doc.docstatus != 0:
        frappe.throw("Cannot edit entries on a submitted timesheet.")

    idx = cint(idx)
    row = next((e for e in doc.timesheet_entry if e.idx == idx), None)
    if not row:
        frappe.throw("Entry not found", frappe.DoesNotExistError)
    if not _is_mine(row, user):
        frappe.throw("You can only edit your own time entries.", frappe.PermissionError)
    if row.get("is_running") and (start_time is not None or end_time is not None):
        frappe.throw("Stop the timer before editing its start or end time.")

    before = f"{row.start_time} to {row.end_time}"
    if entry_date is not None:
        row.entry_date = entry_date
    if start_time is not None:
        row.start_time = start_time
    if end_time is not None:
        row.end_time = end_time

    if duration_hours is not None:
        row.duration_hours = duration_hours
        row.minutes = round(float(duration_hours) * 60, 2)
    elif start_time is not None or end_time is not None:
        if not row.start_time or not row.end_time:
            frappe.throw("Both start and end time are required.")
        duration = round(time_diff_in_hours(row.end_time, row.start_time), 4)
        if flt(duration) <= 0:
            frappe.throw("End time must be after start time.")
        row.duration_hours = duration
        row.minutes = round(duration * 60, 2)

    if notes is not None:
        row.notes = notes

    _recalc_total_hours(doc)
    doc.save()
    _log("Edit Entry", user, doc=doc, details=f"Row {idx}: {before} -> {row.start_time} to {row.end_time}",
         photo=_session_photo())
    frappe.db.commit()
    return _build_timesheet_response(doc, user)


@frappe.whitelist()
def delete_entry(worker_token=None, name=None, idx=None):
    user = _worker(worker_token)
    _check_manual_allowed(user)
    doc = _get_doc_for_worker(name, user)
    if doc.docstatus != 0:
        frappe.throw("Cannot delete entries on a submitted timesheet.")

    idx = cint(idx)
    row = next((e for e in doc.timesheet_entry if e.idx == idx), None)
    if not row:
        frappe.throw("Entry not found", frappe.DoesNotExistError)
    if not _is_mine(row, user):
        frappe.throw("You can only delete your own time entries.", frappe.PermissionError)
    if row.get("is_running"):
        frappe.throw("Stop the timer before deleting this entry.")

    details = f"Row {idx}: {row.start_time} to {row.end_time} ({row.duration_hours} h)"
    doc.timesheet_entry = [e for e in doc.timesheet_entry if e.idx != idx]
    _recalc_total_hours(doc)
    doc.save()
    _log("Delete Entry", user, doc=doc, details=details, photo=_session_photo())
    frappe.db.commit()
    return _build_timesheet_response(doc, user)


# --- Stats ---------------------------------------------------------------------

@frappe.whitelist()
def get_timesheet_stats(worker_token=None):
    user = _worker(worker_token)
    today_str = today()

    values = {}
    scope_clause = _scope_sql(user, values)

    counts = frappe.db.sql(
        f"""
        select count(*) as total,
               sum(case when docstatus = 0 then 1 else 0 end) as active,
               sum(case when docstatus = 1 then 1 else 0 end) as submitted
        from `tab{TS_DOCTYPE}` ts
        where {scope_clause} and ts.docstatus != 2
        """,
        values,
        as_dict=True,
    )[0]

    pairs = _allocated_pairs(user)
    existing = _timesheets_for_wihs({w for w, _ in pairs})
    pending_timesheets = len([p for p in pairs if p not in existing])

    hours_values = dict(values)
    hours_values["today"] = today_str
    hours_values["pt"] = TS_DOCTYPE
    user_scope = "and te.user = %(user)s" if _entry_user_field_exists() else "and 1 = 0"

    hours = frappe.db.sql(
        f"""
        select
            coalesce(sum(case when te.entry_date = %(today)s and te.is_running = 0 {user_scope}
                              then te.duration_hours else 0 end), 0) as today_hours,
            sum(case when te.is_running = 1 {user_scope} then 1 else 0 end) as running_count
        from `tab{ENTRY_DOCTYPE}` te
        inner join `tab{TS_DOCTYPE}` ts on ts.name = te.parent
        where {scope_clause} and ts.docstatus != 2 and te.parenttype = %(pt)s
        """,
        hours_values,
        as_dict=True,
    )[0]

    return {
        "total_timesheets": int(counts.total or 0),
        "active_timesheets": int(counts.active or 0),
        "submitted_timesheets": int(counts.submitted or 0),
        "pending_timesheets": pending_timesheets,
        "total_hours_today": round(flt(hours.today_hours), 2),
        "has_active_timer": bool(hours.running_count),
    }


# --- My Timesheet report -------------------------------------------------------

@frappe.whitelist()
def get_my_timesheet_report(worker_token=None, from_date=None, to_date=None, wih_number=None, status=None):
    user = _worker(worker_token)
    wih_number = (wih_number or "").strip()
    has_range = bool(from_date) and bool(to_date)
    has_wih = bool(wih_number)
    if not has_range and not has_wih:
        frappe.throw("Select a date range or search by WIH number to generate the report.")

    if status == "Draft":
        status_clause = "and et.docstatus = 0"
    elif status == "Submitted":
        status_clause = "and et.docstatus = 1"
    else:
        status_clause = "and et.docstatus in (0, 1)"

    date_clause = "and te.entry_date between %(from_date)s and %(to_date)s" if has_range else ""
    wih_clause = "and et.wih_number like %(wih)s" if has_wih else ""

    if not _entry_user_field_exists():
        return []
    item_col = f"et.{TS_ITEM_FIELD}" if _item_mode() else "null"

    values = {"user": user, "pt": TS_DOCTYPE}
    if has_range:
        values["from_date"] = from_date
        values["to_date"] = to_date
    if has_wih:
        values["wih"] = f"%{wih_number}%"

    rows = frappe.db.sql(
        f"""
        select et.name, te.entry_date, et.wih_number, {item_col} as item_code, et.product_name, et.docstatus, et.final_hrs,
               sum(te.duration_hours) as day_hours
        from `tab{ENTRY_DOCTYPE}` te
        inner join `tab{TS_DOCTYPE}` et on et.name = te.parent
        where te.parenttype = %(pt)s
          and (te.is_running = 0 or te.is_running is null)
          and (te.user = %(user)s or te.user is null or te.user = '')
          {date_clause}
          {wih_clause}
          {status_clause}
        group by te.entry_date, et.name
        order by te.entry_date desc, et.wih_number
        """,
        values,
        as_dict=True,
    )

    return [
        {
            "name": r.name,
            "entry_date": str(r.entry_date) if r.entry_date else None,
            "wih_number": r.wih_number,
            "item_code": r.item_code,
            "product_name": r.product_name,
            "status": _status_label(r.docstatus),
            "docstatus": r.docstatus,
            "final_hrs": r.final_hrs,
            "hours": round(flt(r.day_hours), 2),
        }
        for r in rows
    ]
