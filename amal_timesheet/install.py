import frappe

# (role name, desk access). "Kiosk Device" has NO desk access on purpose: the
# tablet's account can run the kiosk page but can't open the ERPNext /app desk.
ROLES = [
    ("Kiosk Device", 0),
    ("Timesheet Manager", 1),  # already exists on your site (used by timesheet_kiosk) — skipped if so
]


def before_install():
    for role_name, desk_access in ROLES:
        if not frappe.db.exists("Role", role_name):
            frappe.get_doc(
                {"doctype": "Role", "role_name": role_name, "desk_access": desk_access}
            ).insert(ignore_permissions=True)
    frappe.db.commit()


def after_install():
    set_default_settings()
    ensure_custom_fields()


# Photo fields on the Timesheet Entry rows (inside Employee Timesheet).
# Added only if the site doesn't have them yet; existing fields are left alone.
ENTRY_PHOTO_FIELDS = [
    {"fieldname": "photo", "label": "Stop Photo", "fieldtype": "Attach Image",
     "description": "Back-camera photo of the work done, taken when the timer was stopped"},
    {"fieldname": "start_photo", "label": "Start Photo", "fieldtype": "Attach Image",
     "description": "Back-camera photo taken at the first start of the day on this item"},
]


def ensure_custom_fields():
    """Runs after install and after every migrate / deploy."""
    doctype = "Timesheet Entry"
    if not frappe.db.exists("DocType", doctype):
        return
    meta = frappe.get_meta(doctype)
    insert_after = meta.fields[-1].fieldname if meta.fields else None
    for f in ENTRY_PHOTO_FIELDS:
        if meta.has_field(f["fieldname"]):
            insert_after = f["fieldname"]
            continue
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": doctype,
            "insert_after": insert_after,
            "read_only": 1,
            **f,
        }).insert(ignore_permissions=True)
        insert_after = f["fieldname"]
    frappe.db.commit()


def set_default_settings():
    """Writes every Amal Timesheet Settings default explicitly. (Frappe reads a
    never-saved tick-box as 0/off, so relying on the doctype's own defaults
    would leave e.g. manual entries switched off.)"""
    defaults = {
        "worker_session_seconds": 60,
        "max_pin_attempts": 5,
        "lockout_minutes": 15,
        "require_photo": 0,
        "allow_manual_entries": 1,
        "one_timer_per_worker": 1,
    }
    settings = frappe.get_single("Amal Timesheet Settings")
    settings.update(defaults)
    settings.flags.ignore_permissions = True
    settings.save()
    frappe.db.commit()
