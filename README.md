# Amal Timesheet (`amal_timesheet`)

Shared-tablet timesheet for Amal Embroideries, based on the Studio Timesheet
app. One tablet, many workers:

- The tablet lists every allocated WIH; opening one shows the people
  allocated to its items, each with a plain **Start / Stop** button
  (no face check, no PIN).
- **Compulsory back-camera photos** (enforced on the server too):
  - **Start photo** — the first Start of the day on each timesheet (WIH
    item), by anyone. Later Starts that day on that item need none.
    Saved in `Timesheet Entry.start_photo`.
  - **Stop photo** — every Stop. Saved in `Timesheet Entry.photo`.
  - Both are also attached to the Employee Timesheet and logged in
    Amal Timesheet Punch Log (`work_photo`), stamped
    START/STOP · WIH · item · worker · date/time.
- **My Hours / Supervisor** (dashboard, report, manual entries, submit) still
  uses the person's PIN.

Facts:

- Opens at **`https://<your-site>/shared_timesheet`**
- Reads and writes the **same** `Employee Timesheet` / `Timesheet Entry` /
  `Timesheet Allocation` / `Work In Hand` records as the Studio Timesheet app
- **Item-level:** each (WIH, item) has its own Employee Timesheet
  (`Employee Timesheet.item_code`); allocation is per item
  (`Timesheet Allocation.item`).
- Adds `photo` and `start_photo` (Attach Image) to Timesheet Entry as Custom
  Fields if they're missing (on install and every migrate).

For step-by-step setup (no coding needed), see **`SETUP_GUIDE.md`**.

## Repo layout

```
./                         ← repo root (this is what goes on GitHub)
├── README.md
├── SETUP_GUIDE.md
├── license.txt
├── pyproject.toml
├── requirements.txt
└── amal_timesheet/        ← the app module (contains hooks.py)
    ├── hooks.py
    ├── install.py          roles, default settings, Timesheet Entry photo fields
    ├── api.py              all whitelisted methods (tap_start / tap_stop, PIN, timesheets)
    ├── utils.py            PIN rules
    ├── modules.txt
    ├── amal_timesheet/doctype/
    │   ├── amal_timesheet_worker/     PIN for My Hours; Enabled switch
    │   ├── amal_timesheet_punch_log/  audit trail with the start / stop photos
    │   └── amal_timesheet_settings/   timeouts, lockout, manual entries, etc.
    ├── public/             JS / CSS / icons / manifest
    └── www/shared_timesheet.*  the page at /shared_timesheet
```

## How it works

1. **Device login (once):** a supervisor signs the tablet in with an ERPNext
   user that has the **Kiosk Device** role (no desk access).
2. **Start / Stop** on a WIH screen: `tap_start(user, wih, item, activity,
   start_photo)` / `tap_stop(user, timesheet, work_photo)`. Only the signed-in
   tablet can call them; the person must be allocated (or have a running
   entry); an Amal Timesheet Worker record with Enabled unticked blocks them.
3. **My Hours:** PIN → short-lived worker token held only in server memory;
   the same photo rules apply to Start / Stop from there.
4. "First start of the day" = the timesheet has no entry dated today
   (server date).

## License

MIT
