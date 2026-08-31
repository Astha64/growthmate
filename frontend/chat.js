/*
 * chat.js — GrowthMate chat UI. Hardcodes actor:"human" for every /chat
 * request (LLD §11.9 — no actor selector; buyer-agent journey runs via
 * buyer_agent.py, not the UI).
 */

const SESSION_KEY = "growthmate_session";
const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("input");

function getSessionId() {
  let id = localStorage.getItem(SESSION_KEY);
  if (!id) {
    id = "sess-human-" + Math.random().toString(36).slice(2, 10);
    localStorage.setItem(SESSION_KEY, id);
  }
  return id;
}

function appendMessage(role, text) {
  const div = document.createElement("div");
  div.className = "msg " + (role === "user" ? "user" : "bot");
  div.textContent = text;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendBlockNotice() {
  const div = document.createElement("div");
  div.className = "msg bot block";
  div.textContent = "(This action was blocked by the safety guardrail and logged to the audit trail.)";
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function send(message) {
  appendMessage("user", message);
  const typing = appendMessage("bot", "…");

  const body = {
    session_id: getSessionId(),
    actor: "human",
    message: message,
  };

  try {
    const resp = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (typing) typing.remove();
    appendMessage("bot", data.reply || "(no reply)");
    if (data.blocked) appendBlockNotice();
  } catch (err) {
    if (typing) typing.remove();
    appendMessage("bot", "Sorry, something went wrong connecting to the server.");
  }
}

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = "";
  send(text);
});
