// ------------------------------------------------------------------
// Thin wrapper around amal_timesheet.api.*
//
// Two separate "sessions" live on this page:
//   • DEVICE  — the tablet's own ERPNext login (cookie based, long-lived).
//               Kept in localStorage under "stk_*" keys (different from the
//               old app's "tk_*" keys, so the two apps never clash).
//   • WORKER  — who is using the tablet right now, after a correct PIN.
//               Kept ONLY in memory: a refresh, closing the app, switching
//               apps or 60 s of no touch always throws it away.
// ------------------------------------------------------------------

const Store = {
  get csrf() { return localStorage.getItem("stk_csrf") || ""; },
  set csrf(v) { localStorage.setItem("stk_csrf", v || ""); },
  get device() { return localStorage.getItem("stk_device") || ""; },
  set device(v) { localStorage.setItem("stk_device", v || ""); },
  get deviceName() { return localStorage.getItem("stk_device_name") || ""; },
  set deviceName(v) { localStorage.setItem("stk_device_name", v || ""); },
  clear() {
    ["stk_csrf", "stk_device", "stk_device_name"].forEach((k) => localStorage.removeItem(k));
  },
};

const Worker = {
  token: null,
  user: null,
  fullName: null,
  isManager: false,
  set(res) {
    this.token = res.worker_token;
    this.user = res.user;
    this.fullName = res.full_name;
    this.isManager = !!res.is_timesheet_manager;
  },
  clear() { this.token = null; this.user = null; this.fullName = null; this.isManager = false; },
  active() { return !!this.token; },
};

let KioskSettings = {
  worker_session_seconds: 60, require_photo: false, allow_manual_entries: true,
};

async function callMethod(method, args = {}, opts = {}) {
  const res = await fetch(`/api/method/amal_timesheet.api.${method}`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(Store.csrf ? { "X-Frappe-CSRF-Token": Store.csrf } : {}),
    },
    body: JSON.stringify(args),
  });

  const data = await res.json().catch(() => ({}));

  if (!res.ok) {
    const msg = extractMessage(data) || `Request failed (${res.status})`;
    // Worker's PIN session ran out → back to the name screen (device stays signed in).
    if (/session ended/i.test(msg)) {
      Worker.clear();
      const err = new Error(msg);
      err.workerSessionEnded = true;
      throw err;
    }
    // The tablet's own login is gone → back to device sign-in.
    // (Skipped for the sign-in call itself, where a 401 just means a wrong password.)
    if (!opts.isLogin && (res.status === 401 || /invalid request|csrf|not signed in/i.test(msg))) {
      Store.clear();
      Worker.clear();
      const err = new Error("This device was signed out. Please sign the device in again.");
      err.deviceSessionEnded = true;
      throw err;
    }
    throw new Error(msg);
  }
  return data.message;
}

// Worker-scoped call: always attaches the in-memory worker token.
function wcall(method, args = {}) {
  return callMethod(method, { ...args, worker_token: Worker.token });
}

function extractMessage(data) {
  if (!data) return null;
  if (data._server_messages) {
    try {
      const arr = JSON.parse(data._server_messages);
      const msgs = arr
        .map((m) => { try { return JSON.parse(m).message; } catch (e) { return m; } })
        .filter(Boolean)
        .map((m) => String(m).replace(/<[^>]*>/g, ""));
      if (msgs.length) return msgs.join(" ");
    } catch (e) { /* fall through */ }
  }
  if (typeof data.message === "string") return data.message;
  if (data.exception) {
    const parts = String(data.exception).split(":");
    return parts.length > 1 ? parts.slice(1).join(":").trim() : String(data.exception);
  }
  return null;
}

const API = {
  // ---- device ----
  async deviceLogin(usr, pwd) {
    await fetch("/api/method/amal_timesheet.api.reset_session", { credentials: "include" }).catch(() => {});
    Store.csrf = "";
    const res = await callMethod("device_login", { usr, pwd }, { isLogin: true });
    Store.device = res.device_user;
    Store.deviceName = res.device_name;
    Store.csrf = res.csrf_token;
    if (res.settings) KioskSettings = res.settings;
    return res;
  },
  async getDeviceInfo() {
    const res = await callMethod("get_device_info");
    Store.device = res.device_user;
    Store.deviceName = res.device_name;
    if (res.settings) KioskSettings = res.settings;
    return res;
  },
  async deviceLogout() {
    await wcall("device_logout");
    Store.clear();
    Worker.clear();
  },
  isDeviceSignedIn() { return !!Store.device; },

  // ---- workers / PIN ----
  listWorkers() { return callMethod("list_workers"); },
  listKioskTimesheets(search) { return callMethod("list_kiosk_timesheets", { search }); },
  getKioskTimesheet(wih) { return callMethod("get_kiosk_timesheet", { wih }); },
  async verifyPin(user, pin, photo) {
    const res = await callMethod("verify_pin", { user, pin, photo });
    Worker.set(res);
    return res;
  },
  endWorkerSession() {
    const token = Worker.token;
    Worker.clear();
    if (!token) return Promise.resolve();
    return callMethod("end_worker_session", { worker_token: token }).catch(() => {});
  },
  changePin(old_pin, new_pin) { return wcall("change_pin", { old_pin, new_pin }); },

  // ---- timesheets (all worker-scoped) ----
  listWih(search) { return wcall("list_wih", { search }); },
  getStats() { return wcall("get_timesheet_stats"); },
  listTimesheets(status, wih_number, page) { return wcall("list_timesheets", { status, wih_number, page }); },
  createTimesheet(wih_number, start_date, product_name, item_code) {
    return wcall("create_timesheet", { wih_number, start_date, product_name, item_code });
  },
  getTimesheet(name) { return wcall("get_timesheet", { name }); },
  // start_photo: compulsory for the first Start of the day on the item
  startTimer(name, activity, start_photo) { return wcall("start_timer", { name, activity, start_photo }); },
  // work_photo: compulsory back-camera photo of the work done (JPEG data URL)
  stopTimer(name, work_photo) { return wcall("stop_timer", { name, work_photo }); },
  getActivityOptions() { return callMethod("get_activity_options"); },

  // ---- plain tap Start / Stop from the WIH team screen (no PIN) ----
  tapStart(user, wih_number, item_code, activity, start_photo, start_date, product_name) {
    return callMethod("tap_start", { user, wih_number, item_code, activity, start_photo, start_date, product_name });
  },
  tapStop(user, timesheet, work_photo) { return callMethod("tap_stop", { user, timesheet, work_photo }); },
  addEntry(name, entry_date, start_time, end_time, notes) {
    return wcall("add_entry", { name, entry_date, start_time, end_time, notes });
  },
  deleteEntry(name, idx) { return wcall("delete_entry", { name, idx }); },
  updateEntry(name, idx, fields) { return wcall("update_entry", { name, idx, ...fields }); },
  submitTimesheet(name, final_hrs) { return wcall("submit_timesheet", { name, final_hrs }); },
  getMyTimesheetReport(from_date, to_date, wih_number, status) {
    return wcall("get_my_timesheet_report", { from_date, to_date, wih_number, status });
  },
};
