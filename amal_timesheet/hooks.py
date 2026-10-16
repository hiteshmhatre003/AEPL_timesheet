app_name = "amal_timesheet"
app_title = "Amal Timesheet"
app_publisher = "Amal Embroideries Private Limited"
app_description = "Shared-tablet timesheet: tap Start / Stop, with compulsory back-camera start and stop photos."
app_email = "admin@example.com"
app_license = "MIT"

# Creates the "Kiosk Device" (and, if missing, "Timesheet Manager") roles
# BEFORE this app's doctypes are synced, because those doctypes' permission
# rows refer to these roles. after_install fills in default Amal Timesheet Settings.
before_install = "amal_timesheet.install.before_install"
after_install = "amal_timesheet.install.after_install"
# Makes sure Timesheet Entry has the "photo" (stop) and "start_photo" fields.
after_migrate = "amal_timesheet.install.ensure_custom_fields"

# Served at /shared_timesheet — deliberately a DIFFERENT address from the existing
# timesheet_kiosk app (/timesheet), so both apps can live on the same site
# without touching each other. The wildcard rule lets a hard refresh on a
# sub-path (e.g. /shared_timesheet/w/timer/ET-0001) still load the app shell.
website_route_rules = [
    {"from_route": "/shared_timesheet", "to_route": "shared_timesheet"},
    {"from_route": "/shared_timesheet/<path:app_path>", "to_route": "shared_timesheet"},
]
