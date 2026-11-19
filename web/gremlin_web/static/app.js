// Gremlin web. One module, no dependencies; each page is initialised by body[data-page].

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// Server time minus this device's clock, so live timers agree with the server's timestamps.
let skew = 0;
const serverNow = () => (Date.now() + skew) / 1000;

async function api(method, path, body) {
  let response;
  let data = null;
  // A busy site sheds load before running a request (429 without our error body), so retrying
  // that is always safe; the site's own errors carry {"error": ...} and are never retried.
  for (let attempt = 0; ; attempt += 1) {
    try {
      response = await fetch(path, {
        method,
        headers: body === undefined ? {} : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      throw new ApiError(0, "NETWORK", "The site can't be reached. Check the connection and try again.");
    }
    const date = Date.parse(response.headers.get("Date") || "");
    // The header has whole seconds; half a second centres the error.
    if (!Number.isNaN(date)) skew = date + 500 - Date.now();
    if (response.status === 204) return null;
    data = null;
    try {
      data = await response.json();
    } catch {
      /* a gateway error without a body */
    }
    const shed = response.status === 429 && !(data && data.error);
    if (!shed || attempt === 4) break;
    await new Promise((resolve) => setTimeout(resolve, 600 * 2 ** attempt + Math.random() * 400));
  }
  if (!response.ok) {
    const error = data && data.error;
    throw new ApiError(
      response.status,
      error ? error.code : "UNAVAILABLE",
      error ? error.message : "Something went wrong. Try again in a moment.",
    );
  }
  return data;
}

function show(element, message, tone) {
  if (!element) return;
  if (message === null || message === undefined || message === "") {
    element.hidden = true;
    return;
  }
  element.textContent = message;
  if (tone) element.dataset.tone = tone;
  element.hidden = false;
}

function busy(button, on) {
  if (!button) return;
  if (on) {
    button.dataset.state = "loading";
    button.disabled = true;
  } else {
    delete button.dataset.state;
    button.disabled = false;
  }
}

const plural = (n, word) => `${n} ${word}${Math.abs(n) === 1 ? "" : "s"}`;
const when = (seconds) =>
  new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const clock = (seconds) => {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

// The shared counter, and why videos can't start when they can't

const BLOCKED = {
  spending_unauthorized: "Paused",
  endpoint_unconfigured: "Paused",
  daily_limit_reached: "Paused for today",
  dispatch_disabled: "Paused",
  dispatch_held: "Paused",
  budget_exhausted: "Paused",
  unreachable: "Unavailable",
  unconfigured: "Unavailable",
};

function renderHeader(config) {
  const quota = $("[data-quota]");
  const label = `${plural(config.left, "generation")} left`;
  quota.setAttribute("aria-label", label);
  quota.title = label;
  // Ten segments show the share remaining, even if the operator changes the allowance.
  const available = config.total > 0 ? config.left / config.total * 10 : 0;
  $$(".quota-bars span", quota).forEach((bar, index) => {
    bar.style.opacity = 0.2 + 0.8 * Math.max(0, Math.min(1, available - index));
  });
  $("[data-quota-text]").textContent = config.total > 0 ? `${config.left} of ${config.total} left` : "0 left";
  quota.hidden = false;
  const status = $("[data-status]");
  status.hidden = config.gremlin.ready;
  status.textContent = config.gremlin.ready ? "" : BLOCKED[config.gremlin.reason_code] || "Unavailable";
}

async function loadConfig() {
  const config = await api("GET", "/api/v1/config");
  renderHeader(config);
  return config;
}

// One video: preparation, a choice when needed, progress, result, failure

const ACTIVE = ["submitted", "processing", "awaiting_selection"];
const TITLE = {
  awaiting_selection: "Review your video",
  submitted: "Making your video",
  processing: "Making your video",
  succeeded: "Ready",
  failed: "Couldn't make this video",
  cancelled: "Cancelled",
};
// The six stages a video passes through, and which backend stage belongs to each.
const STAGES = [
  ["media", "Preparing media"],
  ["gpu", "Finding a GPU"],
  ["load", "Loading models"],
  ["make", "Generating video"],
  ["output", "Processing output"],
  ["deliver", "Delivering"],
];
const ORDER = STAGES.map(([key]) => key);
const GROUP = {
  analyzing: "media",
  selecting: "media",
  describing: "media",
  waiting: "gpu",
  preparing: "load",
  generating: "make",
  finishing: "output",
  delivering: "deliver",
};
// Why Gremlin isn't taking a video yet: its public admission reasons, and two of the site's own.
const WAITING = {
  busy: "another video is still finishing",
  unavailable: "the GPU service is busy",
  daily_limit_reached: "its daily limit is reached",
  dispatch_disabled: "GPU dispatch is paused",
  dispatch_held: "it is held for a check",
  budget_exhausted: "today's GPU budget is used up",
  spending_limit_reached: "its spending limit is reached",
  spending_unauthorized: "no GPU spending is authorized",
  endpoint_unconfigured: "no GPU is configured",
  unreachable: "it can't be reached",
};
// What each stage is for, under its details.
const ABOUT = {
  media: "On this site's own server: reading your files, picking the shot and characters, then handing them to the GPU service, which checks them.",
  gpu: "A GPU is rented only when someone presses Generate, so nothing is paid while the site is idle. When none is already running, getting one can take a few minutes.",
  load: "The worker loads the model weights, copies your files to the GPU and follows the character through every frame.",
  make: "The video model draws every frame again with your character in place, keeping the clip's motion, camera and scene.",
  output: "Still on the GPU: restoring fine detail, encoding the video and its still, and checking the result.",
  deliver: "This site copies the results into its own private storage, checks them and labels the video as AI-generated.",
};
const START_WAIT_SECONDS = 6 * 3600;
const FPS = 16;
const WHY = {
  auto: "Selected automatically. Tap to change it, or use a candidate button.",
  no_subject: "No confident proposal was found. Tap inside the character you want.",
  unsure: "The detector is unsure. Tap inside the character you want.",
  competing: "There is more than one plausible character. Choose the one you mean.",
  overlap: "The proposed characters overlap. Tap clearly inside the one you mean.",
};
const ASK = { reference: "Reference character", target: "Character to replace" };
// Preparation errors only new files can fix: the interval and description can't help.
const REPLACE_ONLY = ["reference_unreadable", "reference_too_small", "reference_too_large", "driving_unreadable", "driving_timestamps", "public_figure"];
const WHERE = { reference: "in the photo", target: "in the first frame of the video" };

function chooser(gid, side, detail, chosen, onChange, revision) {
  const wrap = document.createElement("div");
  wrap.className = "choose-side";
  const heading = document.createElement("h3");
  heading.textContent = ASK[side];
  const note = document.createElement("p");
  note.className = "small muted";
  note.textContent = WHY[detail.why] || "Review the selected character.";
  const pickWrap = document.createElement("div");
  pickWrap.className = "pick-wrap";
  const picker = document.createElement("div");
  picker.className = "picker";
  const img = document.createElement("img");
  img.alt = side === "reference" ? "Your photo" : "The first frame of your video";
  img.src = `/api/v1/generations/${gid}/preview/${side}?revision=${revision}`;
  const tap = document.createElement("span");
  tap.className = "tap";
  tap.hidden = true;
  picker.append(img, tap);
  const controls = document.createElement("div");
  controls.className = "row candidate-controls";
  controls.setAttribute("aria-label", `${ASK[side]} candidates`);
  const buttons = detail.candidates.map((c, index) => {
    const box = document.createElement("span");
    box.className = "candidate";
    box.textContent = String(index + 1);
    box.setAttribute("aria-hidden", "true");
    const [x0, y0, x1, y1] = c.box;
    Object.assign(box.style, {
      left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
    });
    picker.append(box);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-ghost btn-sm";
    button.textContent = `Candidate ${index + 1}`;
    button.setAttribute("aria-label", `${ASK[side]}, candidate ${index + 1} (${c.label})`);
    button.addEventListener("click", () => set(c.point, c.id));
    controls.append(button);
    return button;
  });
  function draw(point, candidate) {
    tap.hidden = !point;
    if (point) {
      tap.style.left = `${point.x * 100}%`;
      tap.style.top = `${point.y * 100}%`;
    }
    buttons.forEach((button, i) => button.setAttribute("aria-pressed", String(detail.candidates[i].id === candidate)));
  }
  function set(point, candidate) {
    chosen[side] = point;
    chosen[`${side}_candidate`] = candidate;
    draw(point, candidate);
    onChange();
  }
  // Keyboard: arrow keys move the point (Shift for bigger steps) and a live line reads it out.
  picker.tabIndex = 0;
  picker.setAttribute("role", "group");
  picker.setAttribute("aria-label", `${ASK[side]}: use the arrow keys to move the point ${WHERE[side]}`);
  const readout = document.createElement("p");
  readout.className = "visually-hidden";
  readout.setAttribute("aria-live", "polite");
  const STEPS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  picker.addEventListener("keydown", (event) => {
    const step = STEPS[event.key];
    if (!step || picker.closest("fieldset")?.disabled) return;
    event.preventDefault();
    const size = event.shiftKey ? 0.1 : 0.02;
    const from = chosen[side] || detail.point || { x: 0.5, y: 0.5 };
    const clamp = (v) => Math.round(Math.max(0, Math.min(1, v)) * 1000) / 1000;
    const point = { x: clamp(from.x + step[0] * size), y: clamp(from.y + step[1] * size) };
    set(point, null);
    readout.textContent = `Point at ${Math.round(point.x * 100)}% across, ${Math.round(point.y * 100)}% down`;
  });
  picker.addEventListener("click", (event) => {
    if (picker.closest("fieldset")?.disabled) return;
    const rect = img.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width;
    const y = (event.clientY - rect.top) / rect.height;
    if (x < 0 || x > 1 || y < 0 || y > 1 || !rect.width || !rect.height) return;
    set({ x, y }, null);
  });
  draw(detail.point, detail.candidate);
  pickWrap.append(picker);
  wrap.append(heading, note, pickWrap, readout, controls);
  return wrap;
}

// The progress card: six stages, each a button that opens what happened in it. Every line
// comes from a time or a measurement the server recorded (`view.timeline`); nothing is
// simulated, and a time that wasn't recorded is left out rather than estimated.

const timeOfDay = (seconds) =>
  new Date(seconds * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const hourMinute = (seconds) =>
  new Date(seconds * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const span = (seconds) => (seconds < 10 ? `${seconds.toFixed(1)} s` : seconds < 60 ? `${Math.round(seconds)} s` : clock(seconds));
const size = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);
const FORMAT = { quicktime: "MOV", "x-matroska": "MKV", jpeg: "JPEG", heic: "HEIC", heif: "HEIF" };
const format = (type) => {
  const sub = (type || "").split("/")[1] || "";
  return FORMAT[sub] || sub.toUpperCase();
};
const gpuName = (name) => name.replace(/^NVIDIA\s+(GeForce\s+)?/i, "");
const SUBJECT = { auto: "found automatically", tap: "chosen by your tap" };

// The stage a backend stage belongs to, moved on to the furthest one Gremlin's own records
// prove was reached: the site reads Gremlin every 10 to 20 seconds and can miss a short stage.
function groupOf(stage, gremlin, delivery) {
  // The site's `queued` covers sending the files until Gremlin accepts them.
  const own = stage === "queued" ? (gremlin.stages.queued ? "gpu" : "media") : GROUP[stage] || "media";
  let reached = "media";
  if (gremlin.stages.queued) reached = "gpu";
  if (gremlin.gpu_seen_at || gremlin.started_at) reached = "load";
  if (gremlin.stages.running || gremlin.timings.generation_seconds !== undefined) reached = "make";
  if (gremlin.stages.finishing) reached = "output";
  if (delivery.started_at) reached = "deliver";
  return ORDER.indexOf(reached) > ORDER.indexOf(own) ? reached : own;
}

// {key: {state, start, end, duration, events}} for one view of a generation, at `now`.
function stagesOf(g, now) {
  const t = g.timeline;
  const a = t.analysis;
  const r = { stages: {}, timings: {}, gpu_requests: [], ...t.gremlin };
  const timing = r.timings;
  const out = t.delivery;
  const ended = !ACTIVE.includes(g.state);
  const failed = g.state === "failed" || g.state === "cancelled";
  let current = null;
  if (g.state === "awaiting_selection") current = "media";
  else if (!ended) current = groupOf(g.stage, r, out);
  else if (failed) current = groupOf(t.ended_stage, r, out);
  const index = current ? ORDER.indexOf(current) : ORDER.length;

  // The worker starts when its GPU is seen, unless it was only seen when it took the job.
  const bootSeen = Boolean(r.gpu_seen_at && !(r.started_at && r.started_at <= r.gpu_seen_at));
  const loadStart = bootSeen ? r.gpu_seen_at : r.started_at;
  const bounds = {
    media: [g.submitted_at, r.stages.queued],
    gpu: [r.stages.queued || (r.waiting && r.waiting.since), loadStart],
    load: [loadStart, r.stages.running],
    make: [r.stages.running, r.stages.finishing || out.started_at],
    output: [r.stages.finishing, out.started_at],
    deliver: [out.started_at, g.state === "succeeded" ? g.finished_at : null],
  };
  const events = Object.fromEntries(ORDER.map((key) => [key, []]));
  // tone: done, active (still going; `since` keeps a live timer), info, warn, error
  const add = (key, at, text, tone = "done", extra = {}) => events[key].push({ at, text, tone, ...extra });
  const going = (key) => key === current && !ended;

  // Preparing media: the site's own analysis, on its CPU.
  const files = t.files;
  if (files.reference && files.driving) {
    add("media", g.submitted_at, `Received your photo (${format(files.reference.type)}, ${size(files.reference.bytes)}) and video (${format(files.driving.type)}, ${size(files.driving.bytes)})`);
  }
  if (a.started_at) {
    if (a.finished_at) add("media", a.started_at, "Analysed on this site's server, no GPU needed");
    else add("media", a.started_at, "Analysing: finding the shot to use, the character in your photo and who to replace", going("media") ? "active" : "info", { since: a.started_at });
  }
  if (a.attempts > 1) add("media", null, `Analysis restarted after it ran out of time (attempt ${a.attempts} of 2)`, "warn");
  if (a.finished_at) {
    if (a.clip_ms) add("media", a.finished_at, `Used ${span(a.clip_ms / 1000)} of one continuous shot${a.width ? `, at ${a.width} × ${a.height}` : ""}`);
    for (const [side, label] of [["photo", "Reference character"], ["clip", "Who to replace"]]) {
      const s = a[side];
      if (!s) continue;
      const among = s.found > 1 ? ` among ${s.found} candidates` : "";
      add("media", null, `${label}: ${SUBJECT[s.chosen] || "needs your tap"}${among}`, s.chosen ? "done" : "info");
    }
  }
  const choice = t.choice;
  if (choice.asked_at && !(choice.answered_at > choice.asked_at)) {
    add("media", choice.asked_at, "Waiting for your review", g.state === "awaiting_selection" ? "active" : "info", g.state === "awaiting_selection" ? { since: choice.asked_at } : {});
  } else if (choice.answered_at) add("media", choice.answered_at, "Preparation confirmed");
  const d = t.description;
  if (d.at) {
    add("media", d.at, d.model_calls ? `Description ready (${plural(d.model_calls, "model call")} recorded)` : "Description ready (no model call)");
    if (d.tier) add("media", null, `Motion recipe: ${d.tier}`);
  }
  if (r.sent_at) {
    if (r.stages.queued) add("media", r.sent_at, "Sent to the GPU service");
    else add("media", r.sent_at, "Sending to the GPU service and waiting for its checks", going("media") ? "active" : "info", { since: r.sent_at });
  }
  if (r.stages.queued) add("media", r.stages.queued, `Accepted after its media${r.safety_checks ? " and safety" : ""} checks`);

  // Finding a GPU: Gremlin's queue and the GPU session it requests.
  if (r.waiting) {
    const deadline = g.submitted_at + START_WAIT_SECONDS;
    const still = g.stage === "waiting" && !ended;
    add("gpu", r.waiting.since, still ? `The GPU service isn't taking new videos right now: ${WAITING[r.waiting.reason] || r.waiting.reason}. Retrying automatically until ${timeOfDay(deadline)}` : `The GPU service was not taking new videos (${WAITING[r.waiting.reason] || r.waiting.reason})`, still ? "warn" : "info", still ? { since: r.waiting.since } : {});
  }
  if (going("gpu") && r.queue_position) add("gpu", null, `${plural(r.queue_position, "video")} ahead in the queue`, "active");
  r.gpu_requests.forEach((at, i) => {
    add("gpu", at, i === 0 ? "GPU requested from the cloud" : "Requested again: the previous request found no free GPU in time", i === 0 ? "done" : "warn");
  });
  if (going("gpu") && r.stages.queued && !r.queue_position) {
    if (r.gpu_requests.length) add("gpu", null, "Waiting for a GPU to become free", "active", { since: r.gpu_requests[r.gpu_requests.length - 1] });
    else add("gpu", null, "Waiting for the GPU service to request a GPU", "active", { since: r.stages.queued });
  }
  if (r.gpu) add("gpu", r.gpu_seen_at, `GPU assigned: ${r.gpu}`, "success");
  else if (r.started_at) add("gpu", r.started_at, "GPU assigned (its model hasn't been reported)");

  // Loading models: the worker starts, takes the job and prepares it on the GPU.
  if (bootSeen) {
    if (r.started_at) add("load", r.gpu_seen_at, "Worker started: model weights checked, video engine loaded");
    else add("load", r.gpu_seen_at, "Worker starting: checking the model weights and loading the video engine", going("load") ? "active" : "info", { since: r.gpu_seen_at });
  }
  if (r.started_at) {
    const warmth = timing.engine_first_use === 1 ? " (its first video since it started)" : timing.engine_first_use === 0 ? " (a warm worker that has made videos before)" : "";
    add("load", r.started_at, `Worker took your video${warmth}`);
  }
  if (timing.input_transfer_seconds !== undefined) add("load", null, "Copied your files to the GPU", "done", { took: timing.input_transfer_seconds });
  if (timing.tracking_seconds !== undefined) add("load", null, "Followed the character through every frame", "done", { took: timing.tracking_seconds });
  else if (going("load") && r.started_at) add("load", null, "Following the character through every frame", "active", { since: r.started_at });

  // Generating video.
  if (r.stages.running || timing.generation_seconds !== undefined) {
    const length = g.duration_ms ? `${span(g.duration_ms / 1000)} at ${FPS} fps` : "the video";
    if (timing.generation_seconds !== undefined) add("make", r.stages.running || null, `Generated ${length}`, "done", { took: timing.generation_seconds });
    else add("make", r.stages.running, `Generating ${length}, each frame with your character in place`, going("make") ? "active" : "info", { since: r.stages.running });
  }
  if (timing.model_file_load_seconds) {
    add(timing.generation_seconds !== undefined ? "make" : "load", null, `Model files read in ${span(timing.model_file_load_seconds)}, moved into GPU memory in ${span(timing.model_device_load_seconds || 0)}`, "info");
  }

  // Processing output: restoration, encoding and the result's checks, still on the GPU.
  if (r.stages.finishing && going("output")) add("output", r.stages.finishing, "Restoring detail and encoding the video and its still", "active", { since: r.stages.finishing });
  if (timing.restoration_seconds !== undefined) add("output", null, "Restored detail", "done", { took: timing.restoration_seconds });
  if (timing.encoding_seconds !== undefined) add("output", null, "Encoded the video and its still", "done", { took: timing.encoding_seconds });
  if (timing.delivery_seconds !== undefined) add("output", null, "Uploaded the results from the GPU", "done", { took: timing.delivery_seconds });
  if (out.started_at && r.safety_checks) add("output", null, "Passed the output safety check");

  // Delivering: the site copies and checks the results.
  if (out.started_at) {
    if (g.state === "succeeded") {
      add("deliver", out.started_at, "Copied the results to this site's private storage");
      add("deliver", null, "Checked each file's SHA-256 and labelled the video as AI-generated");
      const f = out.files;
      if (f["result.mp4"]) add("deliver", g.finished_at, `Ready: video ${size(f["result.mp4"])}, still ${size(f["poster.jpg"])}`, "success");
    } else {
      add("deliver", out.started_at, "Copying the results to this site's private storage and checking them", going("deliver") ? "active" : "info", { since: out.started_at });
    }
  }

  if (current && t.retry && !ended) {
    add(current, null, `A step failed; retrying automatically at ${timeOfDay(t.retry.at)} (attempt ${t.retry.attempt} of ${t.retry.of})`, "warn");
  }
  if (failed && current) {
    add(current, g.finished_at, g.failure ? g.failure.message : "Stopped.", g.state === "failed" ? "error" : "info");
  }

  const result = {};
  ORDER.forEach((key, i) => {
    let state = i < index ? "done" : i > index ? "todo" : "active";
    if (i === index && failed) state = g.state === "failed" ? "failed" : "stopped";
    let [start, end] = bounds[key];
    if (i === index && failed) end = g.finished_at;
    let duration = null;
    if (start && end && end >= start) duration = end - start;
    else if (state === "active" && start) duration = null; // live, from `start`
    else if (key === "make" && timing.generation_seconds !== undefined) duration = timing.generation_seconds;
    result[key] = { state, start, end, duration, events: events[key] };
  });
  return { stages: result, current };
}

const STATE_LABEL = { done: "done", active: "in progress", todo: "not started", failed: "failed", stopped: "stopped" };

function buildStages(root, open, onToggle) {
  root.replaceChildren(
    ...STAGES.map(([key, name]) => {
      const item = document.createElement("li");
      item.className = "stage";
      item.dataset.key = key;
      item.dataset.state = "todo";
      const head = document.createElement("button");
      head.type = "button";
      head.className = "stage-head";
      head.setAttribute("aria-expanded", "false");
      head.setAttribute("aria-controls", `stage-${key}`);
      const icon = document.createElement("span");
      icon.className = "stage-icon";
      icon.setAttribute("aria-hidden", "true");
      const text = document.createElement("span");
      text.className = "stage-text";
      const label = document.createElement("span");
      label.className = "stage-name";
      label.textContent = name;
      const sub = document.createElement("span");
      sub.className = "stage-sub";
      text.append(label, sub);
      const chip = document.createElement("span");
      chip.className = "stage-chip";
      chip.hidden = true;
      const time = document.createElement("span");
      time.className = "stage-time num";
      const status = document.createElement("span");
      status.className = "visually-hidden";
      const chevron = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      chevron.setAttribute("viewBox", "0 0 16 16");
      chevron.setAttribute("class", "chevron");
      chevron.setAttribute("aria-hidden", "true");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", "m4.5 6.25 3.5 3.5 3.5-3.5");
      chevron.append(path);
      head.append(icon, text, status, chip, time, chevron);
      const body = document.createElement("div");
      body.className = "stage-body";
      body.id = `stage-${key}`;
      body.setAttribute("role", "region");
      body.setAttribute("aria-label", name);
      const inner = document.createElement("div");
      inner.className = "stage-inner";
      const list = document.createElement("ol");
      list.className = "events";
      const note = document.createElement("p");
      note.className = "stage-note";
      note.hidden = true;
      inner.append(list, note);
      body.append(inner);
      item.append(head, body);
      head.addEventListener("click", () => onToggle(key, !open.has(key)));
      return item;
    }),
  );
}

function setOpen(item, on) {
  item.dataset.open = String(on);
  $(".stage-head", item).setAttribute("aria-expanded", String(on));
  $(".stage-body", item).inert = !on;
}

function eventItem(e, fresh) {
  const item = document.createElement("li");
  item.className = `event${fresh ? " is-new" : ""}`;
  item.dataset.tone = e.tone;
  const dot = document.createElement("span");
  dot.className = "event-dot";
  dot.setAttribute("aria-hidden", "true");
  const text = document.createElement("span");
  text.className = "event-text";
  text.textContent = e.text;
  if (e.took !== undefined || e.since) {
    const meta = document.createElement("span");
    meta.className = "event-meta num";
    if (e.since) meta.dataset.since = e.since;
    else meta.textContent = span(e.took);
    text.append(" ", meta);
  }
  item.append(dot, text);
  if (e.at) {
    const time = document.createElement("time");
    time.className = "event-time num";
    time.dateTime = new Date(e.at * 1000).toISOString();
    time.textContent = timeOfDay(e.at);
    item.append(time);
  }
  return item;
}

// A short, restrained burst from `anchor`; skipped entirely for reduced motion.
function confetti(anchor) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || !anchor.animate) return;
  const rect = anchor.getBoundingClientRect();
  const layer = document.createElement("div");
  layer.className = "confetti";
  layer.setAttribute("aria-hidden", "true");
  layer.style.left = `${rect.left + rect.width / 2}px`;
  layer.style.top = `${rect.top + rect.height / 2}px`;
  const colors = ["#0a84ff", "#30d158", "#f5f5f7", "#ffd60a", "#64d2ff"];
  for (let i = 0; i < 34; i += 1) {
    const piece = document.createElement("i");
    piece.style.background = colors[i % colors.length];
    if (i % 3 === 0) piece.style.borderRadius = "50%";
    const angle = -Math.PI / 2 + (Math.random() - 0.5) * Math.PI * 1.5;
    const distance = 40 + Math.random() * 80;
    const x = Math.cos(angle) * distance;
    const y = Math.sin(angle) * distance;
    const turn = (Math.random() - 0.5) * 720;
    piece.animate(
      [
        { transform: "translate(-50%, -50%) scale(0.4)", opacity: 0 },
        { transform: `translate(calc(${x}px - 50%), calc(${y}px - 50%)) rotate(${turn / 2}deg)`, opacity: 1, offset: 0.45 },
        { transform: `translate(calc(${x * 1.15}px - 50%), calc(${y + 70}px - 50%)) rotate(${turn}deg) scale(0.7)`, opacity: 0 },
      ],
      { duration: 1000 + Math.random() * 500, easing: "cubic-bezier(0.22, 1, 0.36, 1)", fill: "forwards" },
    );
    layer.append(piece);
  }
  document.body.append(layer);
  setTimeout(() => layer.remove(), 1700);
}

async function videoPage() {
  loadConfig().catch(() => {});
  const gid = $("[data-gid]").dataset.gid;
  let current = null;
  let ticker = null;
  let shownReview = null;
  let chosen = {};
  const open = new Set();
  const seenEvents = new Set();
  let firstRender = true;
  let searching = false;
  const celebratedKey = `gremlin.gpu.${gid}`;
  const timelineRoot = $("[data-timeline]");
  const toggle = (key, on) => {
    if (on) open.add(key);
    else open.delete(key);
    setOpen($(`[data-key=${key}]`, timelineRoot), on);
  };
  buildStages(timelineRoot, open, toggle);
  const selectedClip = $("[data-selected-clip]");
  selectedClip.addEventListener("error", () => {
    if (current?.state !== "awaiting_selection" || selectedClip.dataset.retried || !selectedClip.getAttribute("src")) return;
    selectedClip.dataset.retried = "true";
    const url = new URL(selectedClip.src, window.location.href);
    url.searchParams.set("fresh", String(Date.now()));
    selectedClip.src = url.href;
  });
  for (const item of $$(".stage", timelineRoot)) setOpen(item, false);

  let lastContact = null;
  let disconnected = false;
  let editing = false;
  async function load() {
    let g;
    try {
      g = await api("GET", `/api/v1/generations/${gid}`);
    } catch (error) {
      if (error.status === 404) {
        $("[data-title]").textContent = "Not found";
        show($("[data-subtitle]"), "It may have been deleted.");
        return;
      }
      disconnected = true;
      show($("[data-error]"), `${error.message} Progress is stale${lastContact ? ` since ${timeOfDay(lastContact)}` : ""}. Reconnecting automatically.`, "error");
      return setTimeout(load, 8000);
    }
    disconnected = false;
    lastContact = serverNow();
    show($("[data-error]"), null);
    const was = current?.state;
    render(g);
    // The shared counter moves when a video is reserved or given back.
    if (was && was !== g.state) loadConfig().catch(() => {});
    if (ACTIVE.includes(g.state)) setTimeout(load, g.state === "awaiting_selection" ? 15000 : 3000);
  }

  function tick() {
    if (!current) return;
    const now = disconnected && lastContact ? lastContact : serverNow();
    const running = ACTIVE.includes(current.state);
    $("[data-elapsed]").textContent = clock((running ? now : current.finished_at || now) - current.submitted_at);
    for (const meta of $$("[data-since]", timelineRoot)) meta.textContent = clock(now - Number(meta.dataset.since));
    for (const item of $$(".stage[data-state=active]", timelineRoot)) {
      const start = Number(item.dataset.start);
      if (start && running) $(".stage-time", item).textContent = clock(now - start);
    }
  }

  function renderTimeline(g) {
    const card = $("[data-progress-card]");
    const running = g.state === "submitted" || g.state === "processing";
    const ended = !ACTIVE.includes(g.state);
    // Videos made before the timeline was recorded have nothing to show once they end.
    card.hidden = !g.timeline || (ended && !g.timeline.analysis.started_at);
    if (card.hidden) return;
    const now = serverNow();
    const { stages, current: active } = stagesOf(g, now);
    let summary = "";
    for (const [key, name] of STAGES) {
      const stage = stages[key];
      const item = $(`[data-key=${key}]`, timelineRoot);
      item.dataset.state = stage.state;
      item.dataset.start = stage.state === "active" && stage.start ? stage.start : "";
      $(".stage-head .visually-hidden", item).textContent = `, ${STATE_LABEL[stage.state]}`;
      const last = stage.events.filter((e) => e.tone !== "info").at(-1) || stage.events.at(-1);
      const live = stage.state === "active" || stage.state === "failed" || stage.state === "stopped";
      $(".stage-sub", item).textContent = live && last ? last.text : "";
      if (live && last) summary = `${name}: ${last.text}`;
      $(".stage-time", item).textContent =
        stage.duration !== null ? (stage.duration < 60 ? span(stage.duration) : clock(stage.duration)) : stage.state === "active" && stage.start && running ? clock(now - stage.start) : "";
      const list = $(".events", item);
      list.replaceChildren(
        ...stage.events.map((e) => {
          const id = `${key}|${e.text}`;
          const fresh = !firstRender && !seenEvents.has(id);
          seenEvents.add(id);
          return eventItem(e, fresh);
        }),
      );
      if (!stage.events.length && stage.state !== "todo" && stage.state !== "active") {
        list.replaceChildren(eventItem({ text: "No details were recorded for this stage.", tone: "info" }, false));
      }
      list.hidden = !list.children.length;
      const note = $(".stage-note", item);
      note.textContent = ABOUT[key];
      note.hidden = false;
      // A failure opens where it happened, once.
      if ((stage.state === "failed" || stage.state === "stopped") && !open.has(key) && !item.dataset.autoOpened) {
        item.dataset.autoOpened = "true";
        toggle(key, true);
      }
    }

    // The GPU's model, and a small celebration when it is assigned while someone watches.
    const gremlin = g.timeline.gremlin;
    const gpuItem = $("[data-key=gpu]", timelineRoot);
    const chip = $(".stage-chip", gpuItem);
    if (active === "gpu" && !gremlin.gpu) searching = true;
    if (gremlin.gpu) {
      const fresh = chip.hidden;
      chip.textContent = gpuName(gremlin.gpu);
      chip.title = gremlin.gpu;
      chip.hidden = false;
      const recent = gremlin.gpu_seen_at && now - gremlin.gpu_seen_at < 30;
      if (fresh && running && (searching || (firstRender && recent)) && !sessionStorage.getItem(celebratedKey)) {
        sessionStorage.setItem(celebratedKey, "1");
        gpuItem.classList.add("is-celebrating");
        chip.classList.add("is-new");
        requestAnimationFrame(() => confetti(chip));
        setTimeout(() => gpuItem.classList.remove("is-celebrating"), 1600);
      }
    }

    const index = active ? ORDER.indexOf(active) : -1;
    if (running || g.state === "awaiting_selection") {
      $("[data-step]").textContent = `Step ${index + 1} of ${STAGES.length} · ${STAGES[index][1]}`;
    } else if (g.state === "succeeded") {
      $("[data-step]").textContent = "How it was made";
      summary = `${gremlin.gpu ? `GPU: ${gremlin.gpu}. ` : ""}Open a stage to see what happened in it.`;
    } else {
      $("[data-step]").textContent = "Where it stopped";
      summary = index >= 0 ? `It stopped while ${STAGES[index][1].toLowerCase()}.` : "";
    }
    // While running, the open stage's own line says what is happening.
    $("[data-now]").textContent = running ? "" : summary;
    $("[data-now]").hidden = running || !summary;
    $("[data-activity]").textContent = running ? summary : "";
    $("[data-progress-foot]").hidden = !running;
    // Cancel works until a GPU picks the job up; after that the row says so instead.
    $("[data-cancel-row]").hidden = !running;
    $("[data-cancel]").hidden = !g.cancellable;
    $("[data-cancel-note]").textContent = g.cancellable
      ? "You can cancel until the GPU starts."
      : "The GPU has started, so it can't be cancelled now.";
    firstRender = false;
  }

  function readyChoice() {
    if (!current?.selection) return;
    const selection = current.selection;
    const mediaReady = selection.can_generate;
    const changed = Math.round(Number($("[data-interval-start]").value) * 1000) !== (selection.interval.start_ms || 0)
      || Math.round(Number($("[data-interval-duration]").value) * 1000) !== (selection.interval.duration_ms || 3000);
    $("[data-interval-dirty]").hidden = !changed;
    const promptLength = $("[data-description]").value.trim().length;
    $("[data-continue]").disabled = editing || changed || promptLength < 20 || promptLength > 2000 || !mediaReady || !Object.entries(selection.sides).every(([side, detail]) => chosen[side] || detail.point);
  }

  function renderChoice(g) {
    const box = $("[data-choose]");
    box.hidden = g.state !== "awaiting_selection";
    if (box.hidden) {
      shownReview = null;
      // The preview link stops working once review ends; a source left set keeps requesting it.
      const preview = $("[data-selected-clip]");
      if (preview.getAttribute("src")) {
        preview.removeAttribute("src");
        preview.load();
      }
      return;
    }
    const selection = g.selection;
    const key = selection.revision;
    // A failed confirmation can return to review without changing the media. Keep in-page
    // edits during ordinary polls, but show the new error and stored choices after recovery.
    const review = `${key}:${selection.until}:${selection.error?.code || ""}`;
    if (review === shownReview) return;
    shownReview = review;
    chosen = {};
    const sides = Object.keys(selection.sides);
    $("[data-choose-title]").textContent = selection.error ? "Preparation needs attention" : selection.required.length ? "Choose the characters" : "Ready to generate";
    $("[data-choose-note]").textContent = `Nothing has been sent to the GPU yet. This waits for you until ${hourMinute(selection.until)}.`;
    $("[data-choose-sides]").replaceChildren(...sides.map((side) => chooser(gid, side, selection.sides[side], chosen, readyChoice, key)));
    const interval = selection.interval;
    $("[data-interval-start]").value = (interval.start_ms || 0) / 1000;
    $("[data-interval-duration]").value = (interval.duration_ms || 3000) / 1000;
    $("[data-interval-note]").textContent = interval.end_ms !== null && interval.end_ms !== undefined
      ? `Using ${(interval.start_ms / 1000).toFixed(3)} to ${(interval.end_ms / 1000).toFixed(3)} seconds of the original video.`
      : "Automatic search checks the first 10 seconds. You can select a later interval within the first minute.";
    const preview = $("[data-selected-clip]");
    $("[data-source-preview]").open = false;
    for (const media of [$("[data-source-clip]"), $("[data-source-image]")]) {
      media.removeAttribute("src");
      media.hidden = true;
    }
    preview.hidden = !sides.length;
    if (sides.length) {
      preview.src = `/api/v1/generations/${gid}/preview/clip?revision=${key}`;
      // The redirect's signed link lapses after a few minutes; ask once for a fresh one.
      preview.dataset.retried = "";
    } else preview.removeAttribute("src");
    $("[data-description]").value = selection.description;
    $("[data-tier]").value = selection.tier;
    $("[data-description-note]").textContent = selection.description_source === "model"
      ? "A model wrote this from your photo and clip. Check it against the selected characters and action."
      : "This is a generic fallback because the model wasn't available. Writing in the character, the action and the setting gives better motion.";
    $("[data-replace-files]").href = `/?replace=${gid}&revision=${key}`;
    // A clip too short from its very first frame can only be replaced as well.
    const code = selection.error?.code;
    const replaceOnly = REPLACE_ONLY.includes(code) || (code === "driving_too_short" && !interval.start_ms);
    $("[data-replace-only]").hidden = !replaceOnly;
    $("[data-replace-only]").href = `/?replace=${gid}&revision=${key}`;
    $("[data-choice-fields]").hidden = replaceOnly;
    $("[data-continue]").hidden = replaceOnly;
    $("[data-prepare]").disabled = !selection.can_revise;
    $("[data-retry]").disabled = !selection.can_revise;
    show($("[data-choose-error]"), selection.error?.message, "error");
    readyChoice();
  }

  function render(g) {
    if (current && g.version < current.version) return;
    current = g;
    const running = g.state === "submitted" || g.state === "processing";
    $("[data-title]").textContent = TITLE[g.state];
    const took = g.finished_at ? ` · took ${clock(g.finished_at - g.submitted_at)}` : "";
    const length = g.duration_ms ? ` · ${g.duration_ms / 1000} s` : "";
    $("[data-subtitle]").textContent = `${when(g.submitted_at)}${length}${took}`;
    renderChoice(g);
    renderTimeline(g);
    if (ACTIVE.includes(g.state)) {
      if (!ticker) ticker = setInterval(tick, 1000);
    } else if (ticker) {
      clearInterval(ticker);
      ticker = null;
    }
    tick();
    if (g.state === "succeeded" && !g.files) {
      $("[data-subtitle]").textContent += " · deleted";
    }
    if (g.state === "succeeded" && g.files) {
      const player = $("[data-player]");
      if (!player.getAttribute("src")) player.src = `/api/v1/generations/${gid}/files/result.mp4`;
      for (const link of $$("[data-download]")) {
        link.href = `/api/v1/generations/${gid}/files/${link.dataset.download}?download=true`;
      }
      if (!preview.open) preview.showModal();
    }
    if (g.state === "failed" || g.state === "cancelled") {
      $("[data-failure-message]").textContent = g.failure ? g.failure.message : "";
      $("[data-failure]").hidden = false;
    }
    $("[data-delete-row]").hidden = ACTIVE.includes(g.state) || g.state === "succeeded";
  }

  async function cancelIt(button) {
    busy(button, true);
    try {
      render(await api("POST", `/api/v1/generations/${gid}/cancel`));
    } catch (error) {
      show($("[data-error]"), error.message, "error");
    } finally {
      busy(button, false);
    }
  }
  const cancel = $("[data-cancel]");
  cancel.addEventListener("click", () => cancelIt(cancel));
  const cancelChoice = $("[data-cancel-choice]");
  cancelChoice.addEventListener("click", () => cancelIt(cancelChoice));

  function lockChoice(on) {
    editing = on;
    $("[data-choice-fields]").disabled = on;
    $("[data-replace-files]").setAttribute("aria-disabled", String(on));
    $("[data-cancel-choice]").disabled = on;
    readyChoice();
  }
  $("[data-replace-files]").addEventListener("click", (event) => { if (editing) event.preventDefault(); });
  $("[data-description]").addEventListener("input", readyChoice);
  for (const input of $$("[data-interval-start], [data-interval-duration]")) input.addEventListener("input", readyChoice);
  $("[data-source-preview]").addEventListener("toggle", () => {
    if (!$("[data-source-preview]").open || !current?.selection) return;
    const animated = current.selection.source_type.startsWith("image/");
    const media = $(animated ? "[data-source-image]" : "[data-source-clip]");
    media.hidden = false;
    if (!media.getAttribute("src")) media.src = `/api/v1/generations/${gid}/preview/source?revision=${current.selection.revision}`;
    $("[data-use-time]").hidden = animated;
  });
  $("[data-use-time]").addEventListener("click", () => {
    $("[data-interval-start]").value = Math.min(60, $("[data-source-clip]").currentTime).toFixed(3);
    readyChoice();
  });
  // The error leads the card, above the buttons that caused it, so it is brought into view.
  function chooseError(message) {
    const box = $("[data-choose-error]");
    show(box, message, "error");
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  const proceed = $("[data-continue]");
  proceed.addEventListener("click", async () => {
    if (!current || current.state !== "awaiting_selection" || editing || proceed.disabled) return;
    const selected = current.selection;
    const body = { revision: selected.revision };
    for (const side of ["reference", "target"]) {
      if (chosen[side]) body[`${side}_point`] = chosen[side];
      if (chosen[`${side}_candidate`] !== null && chosen[`${side}_candidate`] !== undefined) body[`${side}_candidate`] = chosen[`${side}_candidate`];
    }
    const prompt = $("[data-description]").value;
    if (prompt !== selected.description || selected.error) body.prompt = prompt;
    if ($("[data-tier]").value !== selected.tier) body.tier = $("[data-tier]").value;
    lockChoice(true);
    show($("[data-choose-error]"), null);
    try {
      render(await api("POST", `/api/v1/generations/${gid}/select`, body));
      setTimeout(load, 1000);
    } catch (error) {
      chooseError(error.message);
      if ([404, 409, 410].includes(error.status) && error.code !== "ONE_AT_A_TIME") {
        shownReview = null;
        load();
      }
    } finally {
      lockChoice(false);
    }
  });

  async function reprepare(changeInterval) {
    if (!current?.selection || editing) return;
    const body = { revision: current.selection.revision };
    if (changeInterval) {
      if (!$$("[data-interval-start], [data-interval-duration]").every((input) => input.reportValidity())) return;
      body.start_ms = Math.round(Number($("[data-interval-start]").value) * 1000);
      body.duration_ms = Math.round(Number($("[data-interval-duration]").value) * 1000);
    }
    lockChoice(true);
    try {
      render(await api("POST", `/api/v1/generations/${gid}/prepare`, body));
      setTimeout(load, 1000);
    } catch (error) {
      chooseError(error.message);
      if (error.code === "STALE" || error.code === "NOT_WAITING") load();
    } finally {
      lockChoice(false);
    }
  }
  $("[data-prepare]").addEventListener("click", () => reprepare(true));
  $("[data-retry]").addEventListener("click", () => reprepare(false));

  const preview = $("[data-result]");
  const closeResult = $("[data-close-result]");
  let deletingPreview = false;
  async function closePreview() {
    if (deletingPreview) return;
    deletingPreview = true;
    busy(closeResult, true);
    show($("[data-preview-error]"), null);
    try {
      await api("DELETE", `/api/v1/generations/${gid}`);
      const player = $("[data-player]");
      player.pause();
      player.removeAttribute("src");
      player.load();
      preview.close();
      window.location.replace("/");
    } catch (error) {
      show($("[data-preview-error]"), `Could not delete the preview. ${error.message} Close again to retry.`, "error");
      deletingPreview = false;
      busy(closeResult, false);
    }
  }
  closeResult.addEventListener("click", closePreview);
  preview.addEventListener("cancel", (event) => {
    event.preventDefault();
    closePreview();
  });
  // Unloading cannot wait for a response; expiry cleans up undelivered requests.
  window.addEventListener("pagehide", () => {
    if (!preview.open) return;
    fetch(`/api/v1/generations/${gid}`, { method: "DELETE", keepalive: true }).catch(() => {});
    $("[data-player]").removeAttribute("src");
    $("[data-player]").load();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && preview.open) window.location.replace("/");
  });

  // Deleting can't be undone, so it takes a second, explicit press.
  const remove = $("[data-delete]");
  const confirmRow = $("[data-delete-confirm]");
  const confirm = (on) => {
    remove.hidden = on;
    confirmRow.hidden = !on;
    (on ? $("[data-delete-no]") : remove).focus();
  };
  remove.addEventListener("click", () => confirm(true));
  $("[data-delete-no]").addEventListener("click", () => confirm(false));
  const removeForGood = $("[data-delete-yes]");
  removeForGood.addEventListener("click", async () => {
    busy(removeForGood, true);
    try {
      await api("DELETE", `/api/v1/generations/${gid}`);
      window.location.assign("/");
    } catch (error) {
      show($("[data-error]"), error.message, "error");
      busy(removeForGood, false);
    }
  });
  load();
}

// The example: a real photo, clip and result. The clip follows the result so the pair stays in
// step; both play only while visible, and not at all when motion is reduced (posters stay).

function example() {
  const root = $("[data-example]");
  if (!root || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const [clip, result] = $$("video", root);
  result.addEventListener("timeupdate", () => {
    if (Math.abs(clip.currentTime - result.currentTime) > 0.12) clip.currentTime = result.currentTime;
  });
  new IntersectionObserver(([entry]) => {
    for (const video of [clip, result]) {
      if (entry.isIntersecting) video.play().catch(() => {});
      else video.pause();
    }
  }).observe(root);
}

// The studio

const TYPES = {
  heic: "image/heic", heif: "image/heif", jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", webp: "image/webp",
  gif: "image/gif", mp4: "video/mp4", m4v: "video/mp4", mov: "video/quicktime", webm: "video/webm", mkv: "video/x-matroska",
};
const typeOf = (file) => file.type || TYPES[file.name.split(".").pop().toLowerCase()] || "";

function uploadTo(link, file, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(link.method, link.url);
    for (const [name, value] of Object.entries(link.headers)) request.setRequestHeader(name, value);
    request.upload.onprogress = (event) => event.lengthComputable && onProgress(event.loaded);
    request.onload = () => (request.status < 300 ? resolve() : reject(new Error("upload refused")));
    request.onerror = () => reject(new Error("network"));
    request.send(file);
  });
}

// What each drop takes, by the API's names for the two files.
const DROPS = {
  photo: {
    kind: "reference",
    wrong: "That file isn't a photo this site can use. Try a JPG, PNG or HEIC.",
    broken: "This photo can't be opened. Try another JPG, PNG or HEIC.",
  },
  clip: {
    kind: "driving",
    wrong: "That file isn't a clip this site can use. Try an MP4, MOV or GIF.",
    broken: "This clip can't be opened. Try another MP4, MOV or GIF.",
  },
};

// The first bytes of each accepted format, so a renamed file is caught before it is uploaded.
// MP4, MOV and HEIC are ISO media files: a box name at byte 4.
const ISO = (b) => ["ftyp", "moov", "mdat", "wide", "free", "skip"].includes(String.fromCharCode(...b.slice(4, 8)));
const SIGNATURES = {
  "image/jpeg": (b) => b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff,
  "image/png": (b) => b[0] === 0x89 && String.fromCharCode(...b.slice(1, 4)) === "PNG",
  "image/gif": (b) => String.fromCharCode(...b.slice(0, 4)) === "GIF8",
  "image/webp": (b) => String.fromCharCode(...b.slice(0, 4)) === "RIFF" && String.fromCharCode(...b.slice(8, 12)) === "WEBP",
  "image/heic": ISO,
  "image/heif": ISO,
  "video/mp4": ISO,
  "video/quicktime": ISO,
  "video/webm": (b) => b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3,
  "video/x-matroska": (b) => b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3,
};
// Every browser decodes these images, so failing to is a broken file. Videos are different: a
// real container with a codec this browser lacks still works on the server, so it only loses
// its preview.
const DECODED = ["image/jpeg", "image/png", "image/gif", "image/webp"];

// {error}, {unseen: true} (fine, but no preview here) or {} for a file picked into `name`.
async function inspect(name, file, minimumSeconds) {
  const type = typeOf(file);
  const head = new Uint8Array(await file.slice(0, 16).arrayBuffer());
  if (!SIGNATURES[type]?.(head)) return { error: DROPS[name].broken };
  const url = URL.createObjectURL(file);
  try {
    if (type.startsWith("image/")) {
      const img = new Image();
      img.src = url;
      try {
        await img.decode();
        return {};
      } catch {
        return DECODED.includes(type) ? { error: DROPS[name].broken } : { unseen: true };
      }
    }
    const video = document.createElement("video");
    video.muted = true;
    video.preload = "metadata";
    const seconds = await new Promise((resolve) => {
      video.onloadedmetadata = () => resolve(video.duration);
      video.onerror = () => resolve(null);
      video.src = url;
    });
    video.removeAttribute("src");
    if (seconds === null) return { unseen: true };
    // Only a length the browser could read is checked; the server checks again either way.
    if (Number.isFinite(seconds) && seconds < minimumSeconds) {
      return { error: `This clip is ${seconds.toFixed(1)} s long. Use one with at least ${minimumSeconds} s of action.` };
    }
    return {};
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function createPage() {
  const state = { photo: null, clip: null, uploading: null, revision: 0, locked: false, urls: {} };
  example();
  const query = new URLSearchParams(window.location.search);
  const replacing = query.get("replace");
  const replaceRevision = query.get("revision");
  const errorBox = $("[data-error]");
  const submit = $("[data-submit]");
  let config;
  try {
    config = await loadConfig();
  } catch (error) {
    show(errorBox, error.message, "error");
    return;
  }
  const label = replacing ? "Use these files" : "Generate";
  const blockedBox = $("[data-blocked]");

  // Why no new video can start at all; uploads wait too, since they would only be thrown away.
  function blocked() {
    if (config.left <= 0 && !replacing) return "All the free videos are used up for now. Check back later.";
    if (!config.gremlin.ready) return config.gremlin.message || "New videos aren't starting right now. Try again later.";
    if (!config.preparing) return "New videos can't be prepared right now. Try again tomorrow (UTC).";
    if (config.network && config.network.left <= 0 && !replacing) return "This network has used today's videos. Try again tomorrow (UTC).";
    return null;
  }

  // While something blocks or another visitor holds the one slot, the page checks back itself.
  let recheck = null;
  async function refresh() {
    try {
      config = await loadConfig();
    } catch {
      /* the next check or action reports it */
    }
    update();
  }

  function update() {
    // The visitor's own unfinished video comes first: one at a time, theirs included.
    const own = config.active && !replacing ? config.active : null;
    $("[data-active]").hidden = !own;
    if (own) {
      const review = config.active_state === "awaiting_selection";
      $("[data-active-text]").textContent = review ? "Your other video is waiting for your review." : "Your video is being made.";
      $("[data-active-link]").textContent = review ? "Review it" : "View it";
      $("[data-active-link]").href = `/videos/${own}`;
    }
    const reason = own ? null : blocked();
    const waiting = !reason && Boolean(own || (config.busy && !replacing));
    show(blockedBox, reason || (waiting && !own ? "Someone else's video is being made. Generate opens again when it's done." : null));
    const off = state.locked || Boolean(reason);
    for (const element of $$("[data-file]")) element.disabled = off;
    for (const drop of $$("[data-drop]")) drop.toggleAttribute("data-off", Boolean(reason));
    submit.disabled = !state.photo || !state.clip || Boolean(reason) || waiting || submit.dataset.state === "loading";
    submit.textContent = config.left <= 0 && !replacing ? "None left" : waiting && !own ? "Waiting for another video" : label;
    // What this network can still start today, said up front rather than at Generate.
    const network = config.network;
    const networkLine = $("[data-network]");
    networkLine.hidden = !network || replacing || network.left <= 0 || config.left <= 0;
    if (network) networkLine.textContent = `${network.left} of ${network.total} left from your network today`;
    clearTimeout(recheck);
    if (reason || waiting) recheck = setTimeout(refresh, 20000);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });

  function wireDrop(name, receive) {
    const accept = (file) => {
      if (state.locked || blocked()) return;
      if (!config.types[DROPS[name].kind].includes(typeOf(file))) return show(errorBox, DROPS[name].wrong, "error");
      receive(file);
    };
    const drop = $(`[data-drop=${name}]`);
    const input = $(`[data-file=${name}]`);
    input.addEventListener("change", () => input.files[0] && accept(input.files[0]));
    drop.addEventListener("dragover", (event) => {
      event.preventDefault();
      drop.classList.add("is-over");
    });
    drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
    drop.addEventListener("drop", (event) => {
      event.preventDefault();
      drop.classList.remove("is-over");
      if (event.dataTransfer.files[0]) accept(event.dataTransfer.files[0]);
    });
  }

  // A tile names its file instead of previewing it when the browser can't show it (HEIC in
  // Chrome, some MOVs: still fine to send) or when the file is refused (`data-filled="bad"`).
  const notes = Object.fromEntries($$("[data-drop]").map((drop) => [drop.dataset.drop, $("[data-drop-note]", drop).textContent]));
  function named(name, file, note, filled) {
    const drop = $(`[data-drop=${name}]`);
    for (const preview of $$(".drop-preview", drop)) {
      preview.hidden = true;
      preview.removeAttribute("src");
    }
    drop.dataset.filled = filled;
    $("strong", drop).textContent = file.name;
    $("[data-drop-note]", drop).textContent = note;
  }

  function refused(name, file, message) {
    state[name] = null;
    state.revision += 1;
    state.uploading = null;
    $(`[data-file=${name}]`).value = "";
    named(name, file, message, "bad");
    update();
  }

  // Picks are checked asynchronously; only the latest pick on a tile counts.
  const picks = { photo: 0, clip: 0 };
  async function pick(name, file) {
    const turn = ++picks[name];
    const verdict = await inspect(name, file, config.duration_ms[0] / 1000);
    if (turn !== picks[name]) return null;
    if (verdict.error) {
      refused(name, file, verdict.error);
      return null;
    }
    chosen(name, file);
    if (verdict.unseen) named(name, file, "Added · no preview in this browser", "unseen");
    return verdict.unseen ? null : state.urls[name];
  }

  function chosen(name, file) {
    state[name] = file;
    state.revision += 1;
    state.uploading = null;
    if (state.urls[name]) URL.revokeObjectURL(state.urls[name]);
    state.urls[name] = URL.createObjectURL(file);
    const drop = $(`[data-drop=${name}]`);
    drop.dataset.filled = "true";
    $("strong", drop).textContent = name === "photo" ? "Upload photo" : "Upload clip";
    $("[data-drop-note]", drop).textContent = notes[name];
    update();
    upload();
  }

  wireDrop("photo", async (file) => {
    show(errorBox, null);
    if (file.size > config.limits.reference_bytes) return show(errorBox, "That photo is over 15 MB.", "error");
    const url = await pick("photo", file);
    if (!url) return;
    const img = $("[data-photo-preview]");
    img.hidden = false;
    img.src = url;
  });

  wireDrop("clip", async (file) => {
    show(errorBox, null);
    if (file.size > config.limits.driving_bytes) return show(errorBox, "That clip is over 100 MB. Trim it first.", "error");
    const url = await pick("clip", file);
    if (!url) return;
    const video = $("[data-clip-video]");
    const image = $("[data-clip-image]");
    const animated = ["image/gif", "image/webp"].includes(typeOf(file));
    video.hidden = animated;
    image.hidden = !animated;
    (animated ? video : image).removeAttribute("src");
    (animated ? image : video).src = url;
  });

  // Uploads start as soon as both files are chosen, so Generate rarely waits for them.
  function upload() {
    if (!state.photo || !state.clip) return null;
    if (state.uploading) return state.uploading;
    const revision = state.revision;
    const files = { photo: state.photo, clip: state.clip };
    const bar = $("[data-upload-progress]");
    const total = files.photo.size + files.clip.size;
    const sent = { reference: 0, driving: 0 };
    const progress = () => {
      if (revision !== state.revision) return;
      $("div", bar).style.width = `${((sent.reference + sent.driving) / total) * 100}%`;
    };
    const attempt = (async () => {
      const draft = await api("POST", "/api/v1/generations", {
        reference: { bytes: files.photo.size, type: typeOf(files.photo) },
        driving: { bytes: files.clip.size, type: typeOf(files.clip) },
      });
      if (revision === state.revision) bar.hidden = false;
      progress();
      await Promise.all([
        uploadTo(draft.uploads.reference, files.photo, (n) => ((sent.reference = n), progress())),
        uploadTo(draft.uploads.driving, files.clip, (n) => ((sent.driving = n), progress())),
      ]);
      if (revision === state.revision) bar.hidden = true;
      return draft.id;
    })();
    state.uploading = attempt;
    attempt.catch((error) => {
      // A newer file replaced this upload; only the current one reports.
      if (state.uploading !== attempt) return;
      state.uploading = null;
      bar.hidden = true;
      show(errorBox, error instanceof ApiError ? error.message : "The upload didn't finish. Check the connection and try again.", "error");
    });
    return attempt;
  }

  async function uploaded() {
    try {
      return await upload();
    } catch {
      return null;
    }
  }

  function lockFiles(on) {
    state.locked = on;
    update();
  }

  async function generate() {
    if (submit.disabled) return;
    show(errorBox, null);
    const revision = state.revision;
    lockFiles(true);
    busy(submit, true);
    let sending = false;
    try {
      const gid = await uploaded();
      if (!gid || revision !== state.revision) {
        lockFiles(false);
        return;
      }
      sending = true;
      const view = replacing
        ? await api("POST", `/api/v1/generations/${replacing}/prepare`, { revision: replaceRevision, draft_id: gid })
        : await api("POST", `/api/v1/generations/${gid}/submit`);
      window.location.assign(`/videos/${view.id}`);
    } catch (error) {
      // A lost response may already have admitted this exact draft. Keep its files fixed
      // until the user retries the same idempotent request or follows it from history.
      // The site's own refusals (a 503 when Gremlin closes meanwhile) are certain answers.
      const uncertain = sending && (!error.status || (error.status >= 500 && error.code === "UNAVAILABLE"));
      if (!uncertain) lockFiles(false);
      show(errorBox, uncertain ? `${error.message} Retry to recover this same preparation.` : error.message, "error");
      // A refusal may mean the site's state moved on (paused, used up, busy): show it.
      if (!uncertain) refresh();
    } finally {
      busy(submit, false);
      update();
    }
  }
  lockFiles(false);
  submit.addEventListener("click", generate);
  update();
}

const pages = {
  video: videoPage,
  create: createPage,
};
const init = pages[document.body.dataset.page];
if (init) init();
