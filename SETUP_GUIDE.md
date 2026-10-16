# Amal Timesheet (Shared Tablet) — Setup Guide

One tablet on the floor. The tablet lists your timesheets (WIHs). Open one and it shows only the people allocated to it, each with their own **Start** / **Stop** button. A worker just taps their button — no face scan, no PIN.

**Photos (back camera) are compulsory at two moments:**

- **Start photo** — the **first Start of the day on each WIH item**, whoever starts it. Later Starts on that item the same day need no photo. Next morning, the first Start on that item needs a photo again.
- **Stop photo** — **every** Stop.

If the photo isn't taken, the timer does not start or stop. Each photo is stamped with START/STOP, the WIH, item, worker name, date and time.

The tablet page is at `https://your-site.frappe.cloud/shared_timesheet`.

---

## Before you start

You will need:

- Administrator access to ERPNext and to Frappe Cloud.
- Your GitHub account (`hiteshmhatre003`).
- One Android tablet with a **back camera** (a phone also works). Use the **Chrome** browser.
- About 30 minutes.

**This site must already have** the Employee Timesheet (with the **item_code** field), Timesheet Entry, Timesheet Allocation (with the **item** field) and Work In Hand doctypes — the same ones your Studio Timesheet site uses.

Installing the app adds, automatically:

- three doctypes: **Amal Timesheet Worker**, **Amal Timesheet Settings**, **Amal Timesheet Punch Log**;
- the **Kiosk Device** role;
- two photo columns in the **Timesheet Entry** table, if they aren't there yet: **photo** (stop photo) and **start_photo** (start photo). If you already added a `photo` field yourself, it is kept as it is.

Do Steps 1–3 **after working hours** — adding an app restarts your ERPNext site for a few minutes.

---

## Step 1 — Put the app on GitHub

1. Unzip `amal_timesheet.zip` on your computer. You get a folder called `amal_timesheet`.
2. On github.com, click **New repository**. Name it `amal_timesheet`. Do **not** tick "Add a README". Click **Create repository**.
3. On the empty repository page, click **"uploading an existing file"**.
4. Open the `amal_timesheet` folder on your computer, select **everything inside it** (Ctrl+A), and drag it into the GitHub upload box.

   ⚠️ Drag the **contents**, not the folder itself. After uploading, the first page of the repository must show `pyproject.toml`, `README.md` and a folder called `amal_timesheet`.

5. Click **Commit changes**.

## Step 2 — Add the app on Frappe Cloud

1. Log in to Frappe Cloud → open the **bench** that runs the site you want this app on.
2. Go to **Apps** → **Add App** → **Add from GitHub** → choose `amal_timesheet`, branch `main`.
3. Click **Deploy**. Wait until the deploy shows **Success** (usually 5–15 minutes).
4. Go to **Sites** → that site → **Apps** → **Install App** → choose **amal_timesheet**.

## Step 3 — Check it worked

Open this address in your browser:

`https://your-site.frappe.cloud/api/method/amal_timesheet.api.healthz`

You should see: `{"message":{"status":"ok","app":"amal_timesheet"}}`

Then open any **Employee Timesheet** and check the **Timesheet Entry** table has the **photo** and **start_photo** columns.

---

## Step 4 — Create the tablet's own account

The tablet signs in once with its own account. This account cannot open ERPNext and does not count as any worker.

1. ERPNext → search **User** → **+ Add User**.
2. Email: something like `studio.kiosk@amalembroideries.com`. It doesn't need a real inbox.
3. First Name: `Studio Kiosk`. **Untick** "Send Welcome Email". Save.
4. In the user's **Roles** section, tick **only** `Kiosk Device`. Save.
5. Set a strong password. Keep it with the supervisor only.

## Step 5 — Allocate people

Allocate people **per item** in **Timesheet Allocation**: choose the WIH, the **item**, and the people. They appear on that WIH's screen on the tablet straight away and can tap Start / Stop. Each item gets its own Employee Timesheet, created automatically the first time someone starts on it.

**Optional — My Hours / Supervisor screen.** To let someone see their own hours and report on the tablet (and supervisors submit timesheets), add them in **Amal Timesheet Worker** and give them a PIN:

1. ERPNext → search **Amal Timesheet Worker** → **+ Add**.
2. **User**: pick the person. **Set / Reset PIN**: a 4–6 digit PIN (easy PINs like 1234 are refused). Save.

Supervisors also need the **Timesheet Manager** role on their ERPNext user.

**To take someone off the tablet:** remove them from Timesheet Allocation, or untick **Enabled** on their Amal Timesheet Worker record.

## Step 6 — Check the settings

ERPNext → search **Amal Timesheet Settings**. The defaults are sensible.

| Setting | Default | What it means |
|---|---|---|
| Auto Sign-out After | 60 seconds | On the My Hours screens: back to the start if nobody touches the tablet. |
| Wrong PIN Attempts Before Lock | 5 | Then that person's PIN is locked. |
| Lock Duration | 15 minutes | How long the lock lasts (or reset the PIN to unlock now). |
| Require Face Photo at PIN Sign-in (My Hours) | Off | Front-camera photo when someone signs in with their PIN. |
| Allow Workers to Add / Edit / Delete Entries | On | Untick to let only supervisors change times. |
| Only One Running Timer per Worker | On | Stops someone starting WIH-2 while WIH-1 is still running. |

The start and stop photos are always compulsory — there is no setting to switch them off.

---

## Step 7 — Set up the tablet

1. On the tablet, open **Chrome** and go to `https://your-site.frappe.cloud/shared_timesheet`
2. Sign in with the **tablet account** from Step 4 (not your own account).
3. Chrome menu (⋮) → **Add to Home screen**. From now on, open it from that icon.
4. The first time a photo is taken, Chrome asks for camera permission → tap **Allow**.
5. Recommended: **Screen timeout** to the longest option, keep it on charge, and use **App pinning** so workers can't leave the app.
6. Keep the **back** camera lens clean and uncovered — a stand or case must not block it.
7. Don't use this tablet for ERPNext itself. Signing in there would sign the tablet out.

---

## Daily use — for workers

**To start work**

1. On the **Timesheets** screen, tap your WIH.
2. Find your name and tap **Start**.
3. If you're on more than one item of this WIH, choose the item; then choose the activity → **Next**.
4. **If you're the first to start on this item today**, the back camera opens: point it at the work, tap **Take Photo**, then **Use Photo** (or **Retake**).
5. A big **green** screen confirms it.

**To stop work**

1. Tap your WIH, then **Stop** next to your name.
2. The back camera opens. Photograph the work you have done → **Take Photo** → **Use Photo**.
3. A big **red** screen confirms it.

**Cancel** on the photo screen means nothing is started or stopped.

---

## For supervisors — checking the records

**On the Employee Timesheet:** in the **Timesheet Entry** table, each row has its **start_photo** (only on the day's first start for that item) and its **photo** (the stop photo). All photos are also under **Attachments** in the left sidebar.

**Amal Timesheet Punch Log:** every start, stop, edit, submit and wrong PIN is listed with the worker, time, WIH, item, activity and the **Work Photo**. **Done By** shows **Tap** (from the WIH screen) or **PIN** (from My Hours).

---

## Troubleshooting

| Problem | What to do |
|---|---|
| "This device was signed out" | Sign the tablet in again with the tablet account (Step 7). |
| "This account can't run the kiosk" | Give that account the **Kiosk Device** role (Step 4). |
| A person isn't on a WIH's screen | Add them to that WIH item's **Timesheet Allocation**. |
| "Switched off by supervisor" | Their Amal Timesheet Worker record has **Enabled** unticked. |
| "Camera not available" | Chrome → ⋮ → Settings → Site settings → Camera → allow this site. |
| Photo screen shows the front camera | The tablet didn't report a back camera. Check nothing covers the back lens and close other camera apps. |
| "A start photo is required…" / "A photo of the work done is required…" | The photo didn't reach the server. Try again; check the Wi-Fi. |
| No photo / start_photo column in Timesheet Entry | On Frappe Cloud, run **Migrate** on the site once; the app adds them. |
| WIH photo doesn't show on the tablet | ERPNext → **Role Permission Manager** → Work In Hand → add **Kiosk Device** with **Read**. |

---

## Good to know

- **Anyone at the tablet can tap anyone's Start / Stop** — there is no identity check now. The photos and the Punch Log are the record of what happened.
- The day for "first start of the day" follows your ERPNext site's time zone.
