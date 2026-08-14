const state = { photoOffset: 0, photoTotal: 0, history: [], attachedImageId: "" };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function busy(on) { $("#busy").classList.toggle("hidden", !on); }
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.remove("hidden");
  window.setTimeout(() => el.classList.add("hidden"), 5000);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function showView(name) {
  $$(".tab").forEach((el) => el.classList.toggle("active", el.dataset.view === name));
  $$(".view").forEach((el) => el.classList.toggle("active", el.id === `${name}-view`));
}

function imageUrl(item) { return `/api/image/${encodeURIComponent(item.image_id)}`; }

function card(item) {
  const node = $("#card-template").content.firstElementChild.cloneNode(true);
  const img = node.querySelector("img");
  img.src = imageUrl(item);
  img.alt = item.caption || "Indexed photo";
  img.addEventListener("error", () => { img.style.opacity = ".18"; });
  node.querySelector(".caption").textContent = item.caption || item.summary || "Untitled photo";
  const tags = Array.isArray(item.tags) ? item.tags.slice(0, 3) : [];
  const tagBox = node.querySelector(".tags");
  tags.forEach((text) => {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = text;
    tagBox.append(tag);
  });
  node.querySelector(".ask-photo").addEventListener("click", () => attachPhoto(item));
  return node;
}

function renderCards(target, items, replace = true) {
  const box = $(target);
  if (replace) box.replaceChildren();
  if (!items.length && replace) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No matching indexed photos.";
    box.append(empty);
    return;
  }
  items.forEach((item) => box.append(card(item)));
}

function attachPhoto(item) {
  state.attachedImageId = item.image_id;
  const box = $("#attachment");
  box.replaceChildren();
  const label = document.createElement("span");
  label.textContent = `Focused on: ${item.caption || "selected photo"}`;
  const clear = document.createElement("button");
  clear.className = "secondary";
  clear.textContent = "Clear";
  clear.addEventListener("click", clearAttachment);
  box.append(label, clear);
  box.classList.remove("hidden");
  showView("chat");
  $("#chat-input").focus();
}

function clearAttachment() {
  state.attachedImageId = "";
  $("#attachment").classList.add("hidden");
}

function addMessage(role, text, sources = []) {
  const message = document.createElement("div");
  message.className = `message ${role}`;
  const body = document.createElement("div");
  body.textContent = text;
  message.append(body);
  if (sources.length) {
    const sourceBox = document.createElement("div");
    sourceBox.className = "sources";
    sources.slice(0, 5).forEach((source) => {
      if (!source.image_id) return;
      const img = document.createElement("img");
      img.className = "source-thumb";
      img.src = imageUrl(source);
      img.alt = source.caption || "Source photo";
      sourceBox.append(img);
    });
    message.append(sourceBox);
  }
  $("#messages").append(message);
  message.scrollIntoView({ behavior: "smooth", block: "end" });
}

async function health() {
  try {
    const data = await api("/api/health");
    const badge = $("#health");
    badge.textContent = `${data.total_indexed} indexed`;
    badge.classList.add("ok");
  } catch (error) {
    $("#health").textContent = "Mac offline";
    toast(error.message);
  }
}

async function loadPhotos(reset = false) {
  if (reset) state.photoOffset = 0;
  busy(true);
  try {
    const data = await api(`/api/photos?limit=30&offset=${state.photoOffset}`);
    state.photoTotal = data.total_indexed;
    renderCards("#photos", data.items, reset || state.photoOffset === 0);
    state.photoOffset += data.items.length;
    $("#photo-count").textContent = `${state.photoOffset} of ${state.photoTotal}`;
    $("#load-more").classList.toggle("hidden", state.photoOffset >= state.photoTotal || !data.items.length);
  } catch (error) { toast(error.message); }
  finally { busy(false); }
}

$$('.tab').forEach((tab) => tab.addEventListener('click', () => {
  showView(tab.dataset.view);
  if (tab.dataset.view === 'photos' && state.photoOffset === 0) loadPhotos(true);
}));

$("#search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = $("#search-input").value.trim();
  if (!query) return;
  busy(true);
  $("#search-note").textContent = "Searching your multimodal index…";
  try {
    const data = await api("/api/search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, top_k: 10, mode: "auto" }),
    });
    renderCards("#search-results", data.results || []);
    const latency = data.latency_ms ? ` · ${Math.round(data.latency_ms)} ms` : "";
    $("#search-note").textContent = `${(data.results || []).length} results${latency}`;
  } catch (error) { toast(error.message); $("#search-note").textContent = "Search failed"; }
  finally { busy(false); }
});

$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#chat-input");
  const query = input.value.trim();
  if (!query) return;
  input.value = "";
  addMessage("user", query);
  busy(true);
  try {
    const data = await api("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query, top_k: 3, history: state.history.slice(-8),
        attached_image_id: state.attachedImageId,
      }),
    });
    addMessage("assistant", data.answer || "No grounded answer returned.", data.sources || []);
    state.history.push({ role: "user", content: query }, { role: "assistant", content: data.answer || "" });
    if (state.history.length > 8) state.history = state.history.slice(-8);
  } catch (error) { addMessage("assistant", `I couldn't complete that request: ${error.message}`); }
  finally { busy(false); }
});

$("#refresh-photos").addEventListener("click", () => loadPhotos(true));
$("#load-more").addEventListener("click", () => loadPhotos(false));
health();
