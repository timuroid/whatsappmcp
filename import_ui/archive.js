const byId = (id) => document.getElementById(id);
const setText = (id, value) => {
  const element = byId(id);
  if (element.textContent !== value) element.textContent = value;
};
const number = new Intl.NumberFormat("ru-RU");
const date = new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: "Europe/Moscow" });
const time = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short", timeZone: "Europe/Moscow" });
const clock = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow" });
const fileSize = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const timestamp = (seconds) => time.format(seconds * 1000) + " МСК";
const range = (first, last) => date.format(new Date(first)) + " — " + date.format(new Date(last));

let view = document.body.dataset.view;
let status = null;
let pending = false;
let offline = false;
let localError = "";
let archive = [];
let catalog = [];
let catalogState = "idle";
let catalogError = "";
let memberMatchTarget = "";
let watchMatchTarget = "";
let memberMatchSource = "";
let memberMatchChats = [];
let memberMatchState = "idle";
let memberMatchError = "";
let memberMatchSaving = false;
let source = "";
let target = "";
let qrAt = 0;
let archiveKey = "";
let loadedAt = 0;
let updateKey = "";
let targetsKey = "";
let groupKey = "";
let reviewKey = "";
let correcting = "";
const isAdmin = document.body.dataset.role === "admin";
const adminSession = document.body.dataset.adminSession === "true";
const archiveOwner = document.body.dataset.archiveOwner;
const targetSelect = byId("link-target");
const filter = byId("chat-filter");
const qr = byId("pairing-qr");
const groupSelect = byId("group-source");
const viewNames = { home: "Обзор", manual: "Добавить чат", sync: "Источники WhatsApp",
  archive: isAdmin ? "Архив и экспорт" : "Архив", plugin: "Плагин ChatGPT",
  users: "Пользователи", history: "История чата" };
const narrow = window.matchMedia("(max-width: 759px)");
let pairingMethod = isAdmin ? "code" : "qr";
let pairingBusy = false;
let pairingVersion = 0;
let pluginBusy = false;
let pluginState = null;
let pluginStep = null;
let editingPassword = false;
const job = () => status?.job;
const busy = () => pending || Boolean(job());
const connected = () => !offline && status?.connection === "WORKING";
const updates = () => status?.updates || [];
const linkedSources = () => new Set(updates().map((item) => item.source.id));
const candidates = () => {
  const linked = new Set(updates().map((item) => item.target.id));
  return archive.filter((chat) => chat.imported_through &&
    (correcting ? chat.chat_id === correcting : !linked.has(chat.chat_id)));
};
const syncLabels = { scheduled: "Автообновление включено", waiting: "Подбираем чат WhatsApp",
  review: "Администратор проверит", paused: "Автообновление на паузе" };

function apiUrl(path) {
  const url = new URL("/api/" + path, window.location.origin);
  if (archiveOwner && /^\/api\/(?:source(?:\/|$)|admin\/(?:archive|history|attachments)(?:\/|$))/.test(url.pathname))
    url.searchParams.set("owner", archiveOwner);
  return url;
}

async function api(path, data) {
  const response = await fetch(apiUrl(path), data === undefined ? undefined : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (response.status === 401) window.location.replace("/login");
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || "Операция не выполнена. Попробуйте снова.");
  return result;
}

function navigate(event) {
  const previous = view;
  const requested = window.location.hash.slice(1);
  if (Object.hasOwn(viewNames, requested) && byId(requested)?.matches("[data-view-panel]")) view = requested;
  for (const panel of document.querySelectorAll("[data-view-panel]"))
    panel.hidden = panel.dataset.viewPanel !== view;
  for (const link of document.querySelectorAll("[data-view-link]")) {
    if (link.dataset.viewLink === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  setText("current-view", viewNames[view]);
  byId("archive-owner-form").hidden = view === "users" || view === "plugin";
  drawer(false);
  if (view === "plugin" || view === "home") checkPlugin();
  if (view === "users") loadUsers();
  if (event && previous !== view) {
    window.scrollTo(0, 0);
    const heading = byId(view).querySelector("h1");
    heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
  }
  ensureCatalog();
}

function node(tag, content) {
  const element = document.createElement(tag);
  element.textContent = content;
  return element;
}

function setOptions(select, label, items, value) {
  select.replaceChildren(new Option(label, ""));
  for (const item of items) select.add(new Option(item.name, item.chat_id));
  select.value = items.some((item) => item.chat_id === value) ? value : "";
}

async function loadArchive() {
  const { chats } = await api("admin/archive");
  loadedAt = Date.now();
  const key = JSON.stringify(chats);
  if (archiveKey === key) return;
  archiveKey = key;
  archive = chats;
  byId("archive-empty").hidden = Boolean(chats.length);
  byId("archive-rows").replaceChildren(...chats.map((chat) => {
    const row = node("article", "");
    row.className = "archive-row";
    row.append(node("h3", chat.name), node("p", range(chat.first, chat.last)),
      node("small", number.format(chat.count) + " сообщ. · " + number.format(chat.attachments) + " влож." +
        (chat.unavailable ? " · недоступно " + number.format(chat.unavailable) : "")));
    if (chat.imported_through)
      row.append(node("small", "Из файла по " + date.format(new Date(chat.imported_through)) + "."));
    if (chat.shared_group) row.append(node("small", "Общая группа"));
    if (syncLabels[chat.sync_status]) {
      const badge = node("span", syncLabels[chat.sync_status]);
      badge.className = "chip archive-chip";
      badge.dataset.state = chat.sync_status === "scheduled" ? "online" : "warning";
      row.append(badge);
    }
    const link = node("a", "Открыть историю");
    link.href = "#history";
    link.setAttribute("aria-label", "Открыть историю «" + chat.name + "»");
    link.addEventListener("click", () => {
      byId("history-chat").value = chat.chat_id;
      byId("history-query").value = "";
      loadHistory();
    });
    const actions = node("div", "");
    actions.className = "actions chat-actions";
    const remove = node("button", "Удалить");
    remove.type = "button";
    remove.className = "quiet";
    remove.setAttribute("aria-label", "Удалить чат «" + chat.name + "» из архива");
    remove.addEventListener("click", () => confirmChatDeletion(chat.chat_id, chat.name));
    actions.append(link, remove);
    row.append(actions);
    return row;
  }));
  const exportChat = byId("export-chat");
  if (exportChat) {
    setOptions(exportChat, "Все чаты", chats, exportChat.value);
    const historyChat = byId("history-chat");
    const previousChat = historyChat.value;
    setOptions(historyChat, "Выберите чат", chats, previousChat);
    if (previousChat && !historyChat.value) loadHistory();
    byId("export-button").disabled = !chats.length;
  }
  const importTarget = byId("target");
  setOptions(importTarget, "Выберите чат", chats, importTarget.value);
  byId("mode-existing").hidden = !chats.length;
  const resultChip = byId("import-sync-status");
  const imported = resultChip && chats.find((chat) => chat.chat_id === resultChip.dataset.chatId);
  if (imported?.sync_status) {
    resultChip.textContent = syncLabels[imported.sync_status];
    resultChip.dataset.state = imported.sync_status === "scheduled" ? "online" : "warning";
  }
  for (const [stat, field] of [["messages", "count"], ["attachments", "attachments"]]) {
    document.querySelector('[data-stat="' + stat + '"]').textContent =
      number.format(chats.reduce((sum, chat) => sum + chat[field], 0));
  }
  document.querySelector('[data-stat="chats"]').textContent = number.format(chats.length);
  setText("setup-import-status", chats.length ? "Перейти к импорту" : "Загрузить экспорт");
  byId("setup-import").toggleAttribute("data-ready", Boolean(chats.length));
}

let userBusy = false;
let userToDisable = "";
let userAction = null;
let historyRevision = 0;
let historyBefore = null;
let historyLoading = false;
let historyRetryBlocked = false;
let historyQuery = "";

function userButton(label, run) {
  const button = node("button", label);
  button.className = "quiet";
  button.type = "button";
  button.addEventListener("click", run);
  return button;
}

function renderUsers(users, message) {
  const owners = byId("archive-owner");
  const selectedOwner = owners.value;
  owners.replaceChildren(new Option("Мой архив", "admin"),
    ...users.map((user) => new Option(user.name + " · " + user.username, user.username)));
  owners.value = users.some((user) => user.username === selectedOwner) ? selectedOwner : "admin";
  setText("users-feedback", message || (users.length
    ? number.format(users.filter((user) => user.enabled).length) + " с доступом · " + number.format(users.length) + " всего"
    : "Добавьте первую учётную запись."));
  byId("users-list").replaceChildren(...users.map((user) => {
    const row = node("article", "");
    row.className = "user-row";
    const info = node("div", "");
    const state = node("span", user.enabled ? "Доступ открыт" : "Отключён");
    state.className = "chip";
    state.dataset.state = user.enabled ? "online" : "warning";
    info.append(node("h3", user.name), node("small", user.username), state);
    const actions = node("div", "");
    actions.className = "actions";
    actions.append(userButton("Сменить пароль", () => openUserForm(user)),
      userButton(user.enabled ? "Отключить" : "Включить", () => {
        if (!user.enabled) {
          changeUser({ action: "enable", username: user.username }, "Доступ включён.");
          return;
        }
        userToDisable = user.username;
        setText("disable-user-text", "У пользователя «" + user.name + "» завершится вход в архив и подключение ChatGPT.");
        const dialog = byId("disable-user-dialog");
        dialog.returnValue = "cancel";
        dialog.showModal();
      }));
    row.append(info, actions);
    return row;
  }));
}

async function loadUsers() {
  byId("create-user").disabled = true;
  setText("users-feedback", "Загружаем…");
  try {
    const { users } = await api("admin/users");
    renderUsers(users);
    byId("create-user").disabled = false;
  } catch (error) {
    setText("users-feedback", error.message);
  }
}

function openUserForm(user = null) {
  if (userBusy) return;
  userAction = user;
  byId("user-form").reset();
  setText("user-dialog-title", user ? "Сменить пароль · " + user.username : "Создать пользователя");
  setText("save-user", user ? "Сохранить" : "Создать");
  byId("user-form-error").hidden = true;
  byId("user-reset-hint").hidden = !user;
  for (const [field, input] of [["user-name-field", "user-name"], ["user-login-field", "user-login"]]) {
    byId(field).hidden = Boolean(user);
    byId(input).disabled = Boolean(user);
  }
  byId("user-dialog").showModal();
  byId(user ? "user-password" : "user-name").focus();
}

async function changeUser(data, message) {
  if (userBusy) return;
  userBusy = true;
  const dialog = byId("user-dialog");
  const save = byId("save-user");
  save.disabled = true;
  byId("cancel-user").disabled = true;
  try {
    const { users } = await api("admin/users", data);
    renderUsers(users, message);
    byId("user-form").reset();
    if (dialog.open) dialog.close();
  } catch (error) {
    if (dialog.open) {
      setText("user-form-error", error.message);
      byId("user-form-error").hidden = false;
    } else setText("users-feedback", error.message);
  } finally {
    userBusy = false;
    save.disabled = false;
    byId("cancel-user").disabled = false;
  }
}

function historyMessage(message) {
  const row = node("article", "");
  row.className = "history-message";
  const header = node("header", "");
  const sent = new Date(message.timestamp);
  const when = node("time", time.format(sent) + " МСК");
  when.dateTime = message.timestamp;
  header.append(node("strong", message.author || "Автор неизвестен"), when);
  if (message.edited) header.append(node("small", "Изменено"));
  row.append(header);
  if (message.text) row.append(node("p", message.text));
  message.attachments.forEach((file, index) => {
    const details = node("details", "");
    const name = file.filename || file.name || "Вложение";
    details.append(node("summary", name));
    if (file.status === "downloaded") {
      details.append(attachmentLink(message.id, index, "Скачать файл"));
    } else details.append(node("small", "Файл недоступен"));
    if (file.transcript_excerpt) details.append(node("p", file.transcript_excerpt));
    row.append(details);
  });
  if (!message.text && !message.attachments.length) row.append(node("small", "Без текста"));
  return row;
}

const archiveSection = () => isAdmin ? "admin" : "archive";
const conversationViewport = byId("conversation-viewport");
let memberChat = null;

function attachmentLink(id, index, label) {
  const link = node("a", label);
  link.href = apiUrl(archiveSection() + "/attachments/" + encodeURIComponent(id) + "/" + index);
  return link;
}

function conversationAttachment(id, file, index) {
  const name = file.filename || file.name || "Вложение";
  const downloaded = file.status === "downloaded";
  const card = downloaded ? attachmentLink(id, index, "") : node("div", "");
  card.className = "conversation-file";
  const badge = node("span", name.match(/\.([A-Za-z0-9]{1,6})$/)?.[1].toUpperCase() || "ФАЙЛ");
  badge.className = "conversation-file-icon";
  badge.setAttribute("aria-hidden", "true");
  const description = node("span", "");
  description.className = "conversation-file-copy";
  description.append(node("strong", name));
  if (!downloaded) description.append(node("small", "Файл недоступен"));
  else if (Number.isFinite(file.size)) {
    const size = file.size;
    const text = size < 1024 ? size + " Б" : size < 1048576
      ? fileSize.format(size / 1024) + " КБ" : fileSize.format(size / 1048576) + " МБ";
    description.append(node("small", text));
  }
  card.append(badge, description);
  if (downloaded) {
    card.setAttribute("aria-label", "Скачать " + name);
    card.append(byId("conversation-download-icon").content.cloneNode(true));
  }
  if (!file.transcript_excerpt) return card;
  const attachment = node("div", "");
  const transcript = node("details", "");
  transcript.append(node("summary", "Расшифровка"), node("p", file.transcript_excerpt));
  attachment.append(card, transcript);
  return attachment;
}

function conversationMessage(message) {
  const row = node("article", "");
  row.className = "conversation-message";
  const outgoing = message.from_me === true;
  row.toggleAttribute("data-outgoing", outgoing);
  const sent = new Date(message.timestamp);
  const meta = node("header", "");
  meta.className = "conversation-message-meta";
  const when = node("time", clock.format(sent));
  when.dateTime = message.timestamp;
  when.title = time.format(sent) + " МСК";
  meta.append(node("strong", message.author || "Автор неизвестен"), when);
  if (message.edited) meta.append(node("small", "Изменено"));
  if (!outgoing) row.append(meta);
  if (message.text || !message.attachments.length) {
    const body = node("p", message.text || "Без текста");
    body.className = "conversation-message-text";
    row.append(body);
  }
  if (message.attachments.length) {
    const files = node("div", "");
    files.className = "conversation-files";
    files.append(...message.attachments.map((file, index) => conversationAttachment(message.id, file, index)));
    row.append(files);
  }
  if (outgoing) row.append(meta);
  return row;
}

function prependConversationMessages(feed, messages) {
  const fragment = document.createDocumentFragment();
  let day = "";
  for (const message of messages.reverse()) {
    const label = date.format(new Date(message.timestamp));
    if (day !== label) {
      day = label;
      const separator = node("div", label);
      separator.className = "conversation-day";
      separator.dataset.day = day;
      fragment.append(separator);
    }
    fragment.append(conversationMessage(message));
  }
  if (day && feed.firstElementChild?.dataset.day === day) feed.firstElementChild.remove();
  feed.prepend(fragment);
}

function loadEarlierHistory() {
  if (!memberChat) return;
  if (conversationViewport.scrollTop > 240) {
    historyRetryBlocked = false;
    return;
  }
  if (!historyRetryBlocked) loadHistory(true);
}

async function loadHistory(append = false) {
  if (append && (!historyBefore || historyLoading)) return;
  const revision = ++historyRevision;
  const select = byId("history-chat");
  const chat = isAdmin ? select.value : memberChat?.dataset.chatId;
  const feed = byId("history-feed");
  const more = byId("history-more");
  const feedback = byId("history-status");
  const viewport = conversationViewport;
  if (more) more.hidden = true;
  if (!append) {
    historyBefore = null;
    historyRetryBlocked = false;
    historyQuery = isAdmin ? byId("history-query").value.trim() : "";
    feed.replaceChildren();
  }
  if (isAdmin) setText("history-title", chat ? select.selectedOptions[0].textContent : "История чата");
  feedback.hidden = false;
  setText("history-status", !chat ? "Выберите чат." : viewport && append
    ? "Загружаем более ранние сообщения…" : "Загружаем…");
  historyLoading = Boolean(chat);
  if (!chat) return;
  const params = new URLSearchParams({ chat, query: historyQuery });
  if (historyBefore) params.set("before", historyBefore);
  try {
    const result = await api(archiveSection() + "/history?" + params);
    if (historyRevision !== revision) return;
    historyBefore = result.next;
    const anchor = append && viewport ? feed.querySelector("article") : null;
    const oldTop = anchor?.getBoundingClientRect().top;
    if (viewport) prependConversationMessages(feed, result.messages);
    else feed.append(...result.messages.map(historyMessage));
    if (more) more.hidden = !historyBefore;
    feedback.hidden = !isAdmin && Boolean(feed.childElementCount);
    setText("history-status", feed.childElementCount
      ? isAdmin ? "Показано сообщений — " + number.format(feed.childElementCount) : ""
      : historyQuery ? "По этому запросу ничего не найдено." : "В архиве пока нет сообщений этого чата.");
    if (anchor) {
      viewport.scrollTop += anchor.getBoundingClientRect().top - oldTop;
    } else if (viewport) viewport.scrollTop = viewport.scrollHeight;
    if (viewport && historyBefore && viewport.scrollHeight <= viewport.clientHeight)
      requestAnimationFrame(() => { if (historyRevision === revision) loadEarlierHistory(); });
  } catch (error) {
    if (historyRevision !== revision) return;
    if (more) more.hidden = !historyBefore;
    if (viewport && append) historyRetryBlocked = true;
    feedback.hidden = false;
    setText("history-status", error.message);
  } finally {
    if (historyRevision === revision) historyLoading = false;
  }
}

function memberMeta(chat) {
  return "Сообщений — " + number.format(chat.count) + " · С " + chat.first_year + " года";
}

const matchReasons = {
  ambiguous: "В WhatsApp несколько чатов с таким названием.",
  not_found: "В WhatsApp не нашлось чата с таким названием.",
  conflict: "Не удалось настроить автообновление автоматически.",
};
const memberSyncLabels = {
  waiting: "Подбираем чат WhatsApp",
  updating: "Проверяем новые сообщения",
  scheduled: "Автообновление включено",
  paused: "Автообновление на паузе",
  retry: "Ждём повторной попытки обновления",
  ambiguous: "Нужно выбрать источник",
  not_found: "Нужно выбрать источник",
  conflict: "Нужно проверить привязку",
};

function followMemberMatch(chats) {
  const watched = chats.find((chat) => chat.chat_id === watchMatchTarget);
  if (watched && matchReasons[watched.sync_status]) {
    watchMatchTarget = "";
    showMemberMatch(watched);
  } else if (watched && !["waiting", "updating"].includes(watched.sync_status)) {
    watchMatchTarget = "";
  }
  if (memberMatchTarget && !chats.some((chat) => chat.chat_id === memberMatchTarget && matchReasons[chat.sync_status])) {
    closeMemberMatch();
  }
}

async function loadMemberChats() {
  const { chats } = await api("archive");
  followMemberMatch(chats);
  const key = JSON.stringify(chats);
  if (archiveKey === key) return;
  archiveKey = key;
  archive = chats;
  const list = byId("archive-rows");
  const focusedId = list.contains(document.activeElement) ? document.activeElement.dataset.chatId : null;
  const template = byId("member-chat-row").content.firstElementChild;
  list.replaceChildren(...chats.map((chat) => {
    const row = template.cloneNode(true);
    const link = row.querySelector("a");
    link.dataset.chatId = chat.chat_id;
    link.href = "#chat=" + encodeURIComponent(chat.chat_id);
    row.querySelector("[data-chat-name]").textContent = chat.name;
    row.querySelector("[data-chat-meta]").textContent = memberMeta(chat);
    const sync = row.querySelector("[data-chat-sync]");
    sync.textContent = memberSyncLabels[chat.sync_status] || "";
    sync.hidden = !sync.textContent;
    sync.dataset.state = chat.sync_status || "";
    const resolve = row.querySelector(".member-resolve");
    resolve.hidden = !matchReasons[chat.sync_status];
    resolve.addEventListener("click", () => showMemberMatch(chat, true));
    return row;
  }));
  list.hidden = !chats.length;
  setOptions(importTarget, "Выберите чат", chats, importTarget.value);
  byId("mode-existing").hidden = !chats.length;
  if (!chats.length && importMode === "existing") {
    importMode = "new";
    importFields();
  }
  if (focusedId) memberRow(focusedId)?.focus({ preventScroll: true });
  if (memberChat) {
    const updated = memberRow(memberChat.dataset.chatId);
    if (updated) showMemberChat(updated);
    else navigateMember();
  }
  renderMemberUpload();
}

function memberRow(id) {
  return [...byId("archive-rows").querySelectorAll("[data-chat-id]")].find((row) => row.dataset.chatId === id);
}

function showMemberChat(row) {
  memberChat = row;
  setText("history-title", row.querySelector("[data-chat-name]").textContent);
  setText("conversation-meta", row.querySelector("[data-chat-meta]").textContent);
}

const deleteChatDialog = byId("delete-chat-dialog");
const deleteChatButton = byId("confirm-chat-deletion");
const cancelDeleteChat = byId("cancel-chat-deletion");
const deleteChatError = byId("delete-chat-error");
let chatToDelete = "";

function confirmChatDeletion(chatId, name) {
  chatToDelete = chatId;
  setText("delete-chat-name", name);
  deleteChatError.hidden = true;
  deleteChatDialog.showModal();
}

byId("delete-member-chat")?.addEventListener("click", () => {
  if (memberChat) confirmChatDeletion(memberChat.dataset.chatId, byId("history-title").textContent);
});
cancelDeleteChat.addEventListener("click", () => deleteChatDialog.close());
deleteChatDialog.addEventListener("cancel", (event) => {
  if (deleteChatButton.disabled) event.preventDefault();
});
byId("delete-chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (deleteChatButton.disabled) return;
  deleteChatButton.disabled = cancelDeleteChat.disabled = true;
  deleteChatError.hidden = true;
  try {
    const result = await api(isAdmin ? "admin/archive/delete" : "archive/delete", { chat: chatToDelete });
    deleteChatDialog.close();
    if (!isAdmin) {
      history.replaceState(null, "", "#chats");
      navigateMember();
      byId("history-feed").replaceChildren();
    }
    try {
      await (isAdmin ? loadArchive() : loadMemberChats());
    } catch {
      window.location.reload();
      return;
    }
    if (!isAdmin) refreshMemberSource(true);
    const notice = isAdmin ? "archive-delete-notice" : "member-notice";
    setText(notice, result.pending_files ? "Чат удалён. Очистка файлов продолжится автоматически." : "Чат удалён из архива.");
    byId(notice).hidden = false;
  } catch (error) {
    setText("delete-chat-error", error.message);
    deleteChatError.hidden = false;
  } finally {
    deleteChatButton.disabled = cancelDeleteChat.disabled = false;
  }
});

function navigateMember(event) {
  const hash = window.location.hash.slice(1);
  const requested = new URLSearchParams(hash).get("chat");
  const row = requested && memberRow(requested);
  const previous = memberChat;
  byId("chats").hidden = Boolean(row);
  byId("conversation").hidden = !row;
  if (row) {
    showMemberChat(row);
    loadHistory();
  } else {
    historyRevision++;
    memberChat = null;
  }
  if (event || row) {
    if (row) byId("history-title").focus({ preventScroll: true });
    else if (previous) memberRow(previous.dataset.chatId)?.focus();
  }
  if (hash === "plugin") openProfile();
  if (hash === "manual") {
    uploadRequested = true;
    renderMemberUpload();
  }
}

function renderCatalog() {
  const unavailable = linkedSources();
  const query = filter.value.trim().toLocaleLowerCase("ru");
  const visible = catalog.filter((chat) =>
    (chat.name + " " + chat.id).toLocaleLowerCase("ru").includes(query));
  const list = byId("live-chats");
  list.replaceChildren();
  list.setAttribute("aria-busy", String(catalogState === "loading"));
  for (const chat of visible) {
    const row = node("label", "");
    row.className = "chat-option";
    const input = node("input", "");
    input.type = "radio";
    input.name = "source-chat";
    input.checked = chat.id === source;
    input.value = chat.id;
    input.dataset.linked = String(unavailable.has(chat.id));
    input.addEventListener("change", () => { source = chat.id; renderControls(); });
    const kind = chat.kind === "group" ? "Группа" : "Личный чат";
    const suffix = chat.id.split("@")[0].slice(-4);
    row.append(input, node("span", chat.name), node("small", unavailable.has(chat.id) ? "Привязан" : kind + " · " + suffix));
    list.append(row);
  }
  if (!visible.length) {
    const empty = node("p", {
      idle: "Список чатов ещё не загружен.",
      loading: "Получаем список чатов…",
      error: catalogError,
      ready: query ? "Чаты не найдены. Измените запрос." : "WhatsApp пока не вернул чаты. Обновите список.",
    }[catalogState]);
    empty.className = "chat-empty";
    list.append(empty);
  }
  setText("chat-count", catalog.length ? "Найдено " + number.format(visible.length) + " из " + number.format(catalog.length) : "");
  renderControls();
}

async function loadCatalog() {
  if (catalogState === "loading") return;
  catalogState = "loading";
  renderCatalog();
  try {
    ({ chats: catalog } = await api("source/chats"));
    if (!catalog.some((chat) => chat.id === source)) source = "";
    catalogState = "ready";
  } catch (error) {
    catalogState = "error";
    catalogError = error.message + " Обновите список.";
  }
  renderCatalog();
  render();
}

function renderMemberMatchList() {
  const list = byId("member-match-chats");
  const query = byId("member-match-filter").value.trim().toLocaleLowerCase("ru");
  const visible = memberMatchState === "ready" ? memberMatchChats.filter((chat) =>
    (chat.name + " " + chat.id).toLocaleLowerCase("ru").includes(query)) : [];
  list.replaceChildren();
  list.setAttribute("aria-busy", String(memberMatchState === "loading"));
  for (const chat of visible) {
    const row = node("label", "");
    row.className = "chat-option";
    const radio = node("input", "");
    radio.type = "radio";
    radio.name = "member-match-source";
    radio.checked = memberMatchSource === chat.id;
    radio.disabled = Boolean(chat.subscription);
    radio.addEventListener("change", () => {
      memberMatchSource = chat.id;
      renderMemberMatch();
    });
    const kind = chat.kind === "group" ? "Группа" : "Личный чат";
    row.append(radio, node("span", chat.name), node("small", chat.subscription ? "Уже привязан"
      : kind + " · " + chat.id.split("@")[0].slice(-4)));
    list.append(row);
  }
  if (!visible.length) {
    const empty = node("p", {
      idle: "Готовим список чатов…",
      loading: "Получаем список чатов…",
      error: "Список чатов недоступен.",
      ready: query ? "Чаты не найдены." : "WhatsApp пока не вернул чаты.",
    }[memberMatchState]);
    empty.className = "chat-empty";
    list.append(empty);
  }
}

async function loadMemberMatch() {
  if (!memberMatchTarget || memberMatchState === "loading") return;
  const target = memberMatchTarget;
  memberMatchState = "loading";
  memberMatchError = "";
  renderMemberMatchList();
  renderMemberMatch();
  try {
    const { chats } = await api("source/chats?target=" + encodeURIComponent(target));
    if (target !== memberMatchTarget) return;
    memberMatchChats = chats;
    if (!chats.some((chat) => chat.id === memberMatchSource && !chat.subscription)) memberMatchSource = "";
    memberMatchState = "ready";
  } catch (error) {
    if (target !== memberMatchTarget) return;
    memberMatchState = "error";
    memberMatchError = error.message;
  }
  renderMemberMatchList();
  renderMemberMatch();
}

function renderMemberMatch() {
  const panel = byId("member-match");
  panel.hidden = !memberMatchTarget || !connected();
  byId("member-match-submit").disabled = busy() || memberMatchSaving || memberMatchState !== "ready" || !memberMatchSource;
  byId("member-match-refresh").disabled = memberMatchSaving || memberMatchState === "loading";
  setText("member-match-error", memberMatchError);
  byId("member-match-error").hidden = !memberMatchError;
  if (!panel.hidden && memberMatchState === "idle") loadMemberMatch();
}

function closeMemberMatch() {
  memberMatchTarget = "";
  memberMatchSource = "";
  memberMatchChats = [];
  memberMatchState = "idle";
  memberMatchError = "";
  byId("member-match-filter").value = "";
  renderMemberMatch();
}

function showMemberMatch(chat, focus = false) {
  closeMemberMatch();
  memberMatchTarget = chat.chat_id;
  setText("member-match-reason", "«" + chat.name + "». " + matchReasons[chat.sync_status]);
  renderMemberMatch();
  if (focus && connected()) byId("member-match-title").focus();
  else if (focus) byId("connection-panel").scrollIntoView({ block: "nearest" });
}

async function saveMemberMatch() {
  if (!memberMatchTarget || !memberMatchSource || memberMatchSaving || busy()) return;
  memberMatchSaving = true;
  const target = memberMatchTarget;
  memberMatchError = "";
  renderMemberMatch();
  try {
    await api("source/updates/match", { target, source: memberMatchSource });
    if (memberMatchTarget === target) closeMemberMatch();
    await loadMemberChats().catch(() => {});
    refreshMemberSource(true);
  } catch (error) {
    if (memberMatchTarget === target) memberMatchError = error.message;
  } finally {
    memberMatchSaving = false;
    renderMemberMatch();
  }
}

function ensureCatalog() {
  if (isAdmin && view === "sync" && connected() && catalogState === "idle" && !busy()) {
    loadCatalog();
  }
}

function renderControls() {
  const blocked = !connected() || busy() || catalogState !== "ready";
  const selected = catalog.find((chat) => chat.id === source);
  const destination = candidates().find((chat) => chat.chat_id === target);
  byId("link-chat").disabled = blocked || !destination || !selected || linkedSources().has(source);
  byId("refresh-chats").disabled = !connected() || busy() || catalogState === "loading";
  byId("refresh-groups").disabled = byId("refresh-chats").disabled;
  byId("group-sync").disabled = blocked || !groupSelect.value;
  groupSelect.disabled = blocked || groupSelect.options.length <= 1;
  for (const input of byId("live-chats").querySelectorAll("input")) {
    input.disabled = blocked || input.dataset.linked === "true";
  }
  setText("link-boundary", destination
    ? "После " + time.format(new Date(destination.imported_through)) + " МСК"
    : "");
  setText("selection-hint", catalogState === "error" ? catalogError
    : destination && selected ? "«" + selected.name + "» → «" + destination.name + "»."
    : "");
}

function renderGroups() {
  byId("group-panel").hidden = !connected();
  const linked = linkedSources();
  const options = catalog.filter((chat) => chat.kind === "group" && !linked.has(chat.id));
  const key = JSON.stringify(options);
  if (key !== groupKey) {
    groupKey = key;
    setOptions(groupSelect, "Выберите группу", options.map((chat) => ({
      chat_id: chat.id, name: chat.name + " · " + chat.id.split("@")[0].slice(-4),
    })), groupSelect.value);
  }
  setText("group-hint", catalogState === "loading" ? "Загружаем группы…"
    : catalogState === "error" ? catalogError
    : options.length ? "Сначала соберём доступную историю, затем будем обновлять группу."
    : "Добавьте подключённый аккаунт в группу и обновите список.");
}

function selectMapping(chatId, replace = false) {
  correcting = replace ? chatId : "";
  target = chatId;
  source = "";
  filter.value = "";
  targetsKey = "";
  renderCatalog();
  render();
  byId("chat-picker").scrollIntoView({ block: "start" });
  if (!filter.disabled) filter.focus({ preventScroll: true });
}

function renderReview() {
  const labels = { waiting: "Подбор в очереди", ambiguous: "Несколько чатов с таким названием",
    not_found: "В WhatsApp не найден", unverified: "Недостаточно совпадающих сообщений",
    conflict: "Совпадение уже занято или дата файла требует проверки" };
  const matches = status?.matches || {};
  const entries = archive.filter((chat) => matches[chat.chat_id]);
  byId("match-review").hidden = !entries.length;
  const key = JSON.stringify([entries.map((chat) => [chat.chat_id, chat.name, matches[chat.chat_id]]), connected(), busy()]);
  if (key === reviewKey) return;
  reviewKey = key;
  byId("match-rows").replaceChildren(...entries.map((chat) => {
    const row = node("article", "");
    row.className = "archive-row update-row";
    const match = matches[chat.chat_id];
    const choose = node("button", "Выбрать чат");
    choose.type = "button";
    choose.className = "secondary";
    choose.disabled = !connected() || busy();
    choose.addEventListener("click", () => selectMapping(chat.chat_id));
    row.append(node("h3", chat.name), node("small", labels[match.status]), choose);
    return row;
  }));
}

function renderUpdates() {
  const links = updates();
  byId("updates-panel").hidden = !links.length;
  byId("update-now").disabled = busy() || !connected() || !links.some((item) => item.enabled);
  const snapshot = JSON.stringify([links, connected(), busy(), status?.updates_retry_at]);
  if (snapshot === updateKey) return;
  const existingSources = JSON.stringify([...linkedSources()]);
  if (existingSources !== renderUpdates.sources) {
    renderUpdates.sources = existingSources;
    if (linkedSources().has(source)) source = "";
    renderCatalog();
  }
  updateKey = snapshot;
  byId("update-rows").replaceChildren(...links.map((item) => {
    const row = node("article", "");
    row.className = "archive-row update-row";
    let label = "На паузе";
    if (item.enabled) {
      const next = Math.max(item.next_at, status.updates_retry_at || 0);
      label = connected() ? (next <= Date.now() / 1000 ? "Ожидает запуска" : "Следующая проверка " + timestamp(next))
        : "Ожидает подключения WhatsApp";
    }
    row.append(node("h3", item.target.name), node("p", label),
      node("small", item.mode === "export" ? "Из «" + item.source.name + "» после " + timestamp(item.floor - 1) + "."
        : (item.source.kind === "group" ? "Группа" : "Личный чат") + " · доступная история"));
    if (item.last) row.append(node("small", "Последняя проверка " + timestamp(item.last.at) + ". Добавлено или обновлено " +
      number.format(item.last.saved) + " · недоступно вложений " + number.format(item.last.unavailable) + "."));
    if (item.error && item.enabled) row.append(node("small", item.error));
    const button = node("button", item.enabled ? "Пауза" : "Возобновить");
    button.type = "button";
    button.className = "secondary";
    button.disabled = busy();
    button.setAttribute("aria-label", button.textContent + " для чата " + item.target.name);
    button.addEventListener("click", () => submit("updates/" + (item.enabled ? "pause" : "resume"), { target: item.target.id }));
    row.append(button);
    if (item.mode !== "group" && item.target.id !== item.source.id) {
      const change = node("button", "Исправить привязку");
      change.type = "button";
      change.className = "quiet";
      change.disabled = busy() || !connected();
      change.addEventListener("click", () => selectMapping(item.target.id, true));
      row.append(change);
    }
    return row;
  }));
}

function render() {
  const online = connected();
  const pairing = !offline && status?.connection === "SCAN_QR_CODE";
  const mismatch = !offline && status?.connection === "ACCOUNT_MISMATCH";
  const starting = job() === "connect" || status?.connection === "STARTING";
  const waiting = !status || offline;
  const connectionLabel = waiting ? "Проверяем связь" : online ? "Подключён"
    : pairing ? "Ожидает привязки" : starting ? "Подключаем WhatsApp"
    : mismatch ? "Другой номер" : "WhatsApp отключён";
  setText("connection-title", connectionLabel);
  byId("connection-title").dataset.state = online ? "online" : "warning";
  byId("connect-actions").hidden = waiting || online || pairing || starting || mismatch;
  byId("connect").disabled = busy();
  byId("account-mismatch").hidden = !mismatch;
  byId("reconnect-source").hidden = !(online || mismatch);
  byId("reconnect-source").disabled = busy();
  byId("renew-qr").disabled = busy() || pairingBusy;
  byId("pairing").hidden = !pairing;
  byId("pair-by-phone").hidden = pairingMethod !== "code";
  byId("pair-by-qr").hidden = pairingMethod !== "qr";
  byId("pair-phone").querySelector("button").disabled = !pairing || busy() || pairingBusy;
  if (pairing && pairingMethod === "qr" && Date.now() - qrAt > 12000) {
    qrAt = Date.now();
    qr.src = apiUrl("source/qr?v=" + qrAt);
  } else if ((!pairing || pairingMethod !== "qr") && qrAt) {
    qrAt = 0;
    qr.removeAttribute("src");
    qr.hidden = true;
    byId("pairing-placeholder").hidden = false;
  }
  if (!pairing && !offline) {
    setText("pair-code", "");
    byId("pair-result").hidden = true;
    byId("pair-error").hidden = true;
    if (online) byId("phone-number").value = "";
  }
  const message = localError || (offline ? "Связь с панелью потеряна. Восстановим автоматически." : status?.error);
  byId("live-error").hidden = !message;
  setText("live-error", message || "");
  if (!isAdmin) {
    renderMemberUpload();
    renderMemberMatch();
    return;
  }
  setText("connection-chip", online ? "WhatsApp подключён" : connectionLabel);
  byId("connection-chip").dataset.state = online ? "online" : "warning";
  const activeLinks = updates().filter((item) => item.enabled).length;
  const review = archive.some((chat) => chat.sync_status === "review");
  setText("setup-sync-status", online ? (activeLinks ? "Обновляется чатов — " + number.format(activeLinks)
    : review ? "Администратор проверит" : "Подключается после импорта")
    : waiting ? "Проверяем связь…" : "Подключить WhatsApp");
  byId("setup-sync").toggleAttribute("data-ready", online && activeLinks > 0);
  const options = candidates();
  const updatesExist = Boolean(updates().length);
  byId("sync-empty").hidden = !online || !archiveKey || options.length > 0 || updatesExist;
  byId("chat-picker").hidden = !online || !options.length;
  setText("mapping-heading", correcting ? "Исправить привязку" : "Привязка чата");
  setText("link-chat", correcting ? "Сохранить новую привязку" : "Включить автообновление");
  byId("cancel-mapping").hidden = !correcting;
  byId("replace-hint").hidden = !correcting;
  const optionKey = JSON.stringify(options);
  if (optionKey !== targetsKey) {
    targetsKey = optionKey;
    setOptions(targetSelect, "Выберите импортированный чат", options, target || (options.length === 1 ? options[0].chat_id : ""));
    target = targetSelect.value;
  }
  renderUpdates();
  renderGroups();
  renderReview();
  renderControls();
  const progress = status?.progress || {};
  byId("live-job").hidden = job() !== "automatic";
  if (job() === "automatic") {
    const bar = byId("job-progress");
    if (progress.total) {
      bar.max = progress.total;
      bar.value = progress.current - 1;
    } else bar.removeAttribute("value");
    setText("job-detail", progress.total ? "Чат " + progress.current + " из " + progress.total +
      " · " + progress.chat : "Готовим обновление…");
  }
  ensureCatalog();
}

async function submit(action, payload = {}) {
  if (busy()) return;
  pending = true;
  localError = "";
  render();
  try {
    await api("source/" + action, payload);
    if (action === "renew-qr") qrAt = 0;
    if (action === "reconnect") {
      catalogState = "idle";
      catalog = [];
      source = "";
      if (isAdmin) renderCatalog();
      else closeMemberMatch();
    }
    status = await api("source");
    offline = false;
    if (!isAdmin) nextMemberPollAt = 0;
    if (isAdmin && action === "updates/run" && !job()) await loadArchive();
    return true;
  } catch (error) {
    localError = error.message;
    return false;
  } finally {
    pending = false;
    render();
  }
}

function drawer(open) {
  const visible = open && narrow.matches;
  byId("navigation").toggleAttribute("data-open", visible);
  byId("nav-overlay").hidden = !visible;
  byId("open-nav").setAttribute("aria-expanded", String(visible));
  document.querySelector(".frame").inert = visible;
  if (visible) byId("close-nav").focus();
}

function closeDrawer() {
  drawer(false);
  byId("open-nav").focus();
}

if (isAdmin) {
  byId("open-nav").addEventListener("click", () => drawer(true));
  byId("close-nav").addEventListener("click", closeDrawer);
  byId("nav-overlay").addEventListener("click", closeDrawer);
  byId("nav-overlay").tabIndex = -1;
  narrow.addEventListener("change", () => drawer(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && byId("navigation").hasAttribute("data-open")) closeDrawer();
  });
  for (const link of byId("navigation").querySelectorAll("a"))
    link.addEventListener("click", () => { if (narrow.matches) closeDrawer(); });
}

function theme(value) {
  document.documentElement.dataset.theme = value;
  byId("theme-toggle").setAttribute("aria-label", value === "light" ? "Включить тёмную тему" : "Включить светлую тему");
}
try { theme(localStorage.getItem("wa-admin-theme") === "light" ? "light" : "dark"); } catch { theme("dark"); }
byId("theme-toggle").addEventListener("click", () => {
  const value = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  theme(value);
  try { localStorage.setItem("wa-admin-theme", value); } catch { /* Theme stays active for this page. */ }
});

function selectPairing(method) {
  pairingMethod = method;
  pairingVersion++;
  byId("method-code").setAttribute("aria-pressed", String(method === "code"));
  byId("method-qr").setAttribute("aria-pressed", String(method === "qr"));
  render();
}
byId("method-code")?.addEventListener("click", () => selectPairing("code"));
byId("method-qr")?.addEventListener("click", () => selectPairing("qr"));
byId("pair-phone")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pairingBusy) return;
  pairingBusy = true;
  const version = ++pairingVersion;
  const button = event.currentTarget.querySelector("button");
  button.textContent = "Запрашиваем код…";
  byId("pair-result").hidden = true;
  byId("pair-error").hidden = true;
  setText("pair-feedback", "");
  render();
  try {
    const result = await api("source/pair-code", { phone: byId("phone-number").value.trim() });
    if (version === pairingVersion && status?.connection === "SCAN_QR_CODE") {
      setText("pair-code", result.code);
      byId("pair-result").hidden = false;
    }
  } catch (error) {
    if (version === pairingVersion) {
      setText("pair-error", error.message);
      byId("pair-error").hidden = false;
    }
  } finally {
    pairingBusy = false;
    button.textContent = byId("pair-result").hidden ? "Получить код" : "Получить новый код";
    render();
  }
});

async function copyText(value, feedback) {
  try {
    await navigator.clipboard.writeText(value);
    feedback.textContent = "Скопировано.";
  } catch {
    feedback.textContent = "Скопируйте вручную.";
  }
}
byId("copy-pair-code")?.addEventListener("click", () => copyText(byId("pair-code").textContent, byId("pair-feedback")));

const examples = {
  price: ["Где согласовали стоимость доставки?", "Снабжение · Мария · 12 марта",
    "Доставку подтвердили. Стоимость — 18 500 ₽."],
  deadline: ["К какому сроку заказ должен быть на складе?", "Поставки · Артём · 19 марта",
    "Партию ждём на складе к пятнице, до 14:00."],
  file: ["В каком чате прислали договор поставки?", "Документы · Елена · 21 марта",
    "Прикрепляю подписанный договор. Договор_поставки.pdf"],
};
for (const demo of document.querySelectorAll("[data-demo]")) {
  const question = demo.querySelector("[data-question]");
  const feedback = demo.querySelector(".demo-feedback");
  const choices = demo.querySelectorAll("[data-example]");
  for (const choice of choices) {
    choice.addEventListener("click", () => {
      for (const item of choices) item.setAttribute("aria-pressed", String(item === choice));
      const [prompt, source, excerpt] = examples[choice.dataset.example];
      question.textContent = prompt;
      demo.querySelector("[data-source]").textContent = source;
      demo.querySelector("[data-excerpt]").textContent = excerpt;
      feedback.textContent = "";
    });
  }
  demo.querySelector("[data-copy-question]").addEventListener("click", () =>
    copyText(question.textContent + " Покажи чат, автора и дату.", feedback));
}

const passwordForm = byId("plugin-password-form");
const pluginCheck = byId("check-plugin");
const password = byId("plugin-password");
const repeat = byId("plugin-password-repeat");
const revoke = byId("plugin-revoke");
const revokeDialog = byId("revoke-dialog");
const endpoint = byId("plugin-endpoint");

function pluginFeedback(message) {
  setText("plugin-feedback", message);
  byId("plugin-feedback").hidden = !message;
}

function renderPlugin() {
  const state = pluginState;
  const hasPassword = Boolean(state?.password_set);
  const named = Boolean(state?.named_users);
  const canSignIn = named || hasPassword;
  const issued = Boolean(state?.sessions);
  const webReady = canSignIn && state.running && !state.local;
  if (named && (!pluginStep || pluginStep === "password")) pluginStep = issued ? "try" : "connect";
  const chip = byId("plugin-password-state");
  chip.dataset.state = canSignIn ? "online" : "warning";
  setText("plugin-password-state", issued ? "Доступ выдан" : named ? "Именной доступ" : hasPassword ? "Пароль задан" :
    state ? "Пароль не задан" : "Проверяем доступ");
  if (isAdmin) {
    setText("setup-plugin-status", issued ? "Доступ выдан" : webReady ? "Добавить в ChatGPT" :
      canSignIn ? "Настроить подключение" : state ? "Задать пароль" : "Проверяем доступ…");
    byId("setup-plugin").toggleAttribute("data-ready", issued);
  }
  byId("plugin-password-actions").hidden = editingPassword || !hasPassword;
  byId("edit-plugin-password").hidden = !adminSession;
  passwordForm.hidden = !state || (hasPassword && !editingPassword);
  byId("cancel-plugin-password").hidden = !hasPassword;
  setText("password-hint", hasPassword ? "От 12 символов. Смена пароля завершит подключения."
    : "От 12 символов. Понадобится при входе из ChatGPT.");
  revoke.hidden = !adminSession || !issued;
  byId("plugin-runtime").dataset.state = state?.running ? "online" : "warning";
  setText("plugin-runtime", state?.running ? "Сервер работает" : state ? "Сервер недоступен" : "Проверяем сервер");
  endpoint.value = state?.endpoint || "";
  byId("copy-endpoint").disabled = !endpoint.value;
  byId("plugin-account-hint").hidden = !canSignIn;
  setText("plugin-transport", state?.local ? "Для ChatGPT Web нужен HTTPS или туннель с OAuth."
    : state && !canSignIn ? "Сначала задайте пароль." : "");
  byId("plugin-path").hidden = !webReady;
  byId("open-chatgpt").hidden = !webReady;
  for (const button of document.querySelectorAll("[data-plugin-step]")) {
    const step = button.dataset.pluginStep;
    button.hidden = named && step === "password";
    button.querySelector("b").textContent = (step === "try" ? (named ? 2 : 3) : step === "connect" && !named ? 2 : 1);
    const active = step === (pluginStep || "password");
    if (active) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
    button.toggleAttribute("data-done", button.dataset.pluginStep === "password" ? hasPassword
      : button.dataset.pluginStep === "connect" && issued);
  }
  for (const panel of document.querySelectorAll("[data-plugin-panel]"))
    panel.hidden = panel.dataset.pluginPanel !== (pluginStep || "password");
}

async function checkPlugin() {
  if (pluginCheck.disabled) return false;
  pluginCheck.disabled = true;
  try {
    pluginState = await api("plugin");
    if (!pluginStep) pluginStep = pluginState.sessions ? "try" : pluginState.password_set ? "connect" : "password";
    renderPlugin();
    return true;
  } catch (error) {
    pluginFeedback(error.message);
    if (isAdmin) setText("setup-plugin-status", "Проверить доступ");
    return false;
  } finally {
    pluginCheck.disabled = false;
  }
}

async function changePlugin(action, data, message) {
  if (pluginBusy) return;
  pluginBusy = true;
  const save = passwordForm.querySelector('button[type="submit"]');
  save.disabled = true;
  revoke.disabled = true;
  pluginFeedback("Сохраняем…");
  try {
    const updated = await api("plugin/" + action, data);
    pluginState = { ...pluginState, ...updated };
    passwordForm.reset();
    editingPassword = false;
    if (action === "password") pluginStep = "connect";
    renderPlugin();
    pluginFeedback(message);
    if (action === "password" && (view === "plugin" || byId("profile-dialog")?.open))
      byId("plugin-connect-heading").focus();
    await checkPlugin();
  } catch (error) {
    pluginFeedback(error.message);
  } finally {
    pluginBusy = false;
    save.disabled = false;
    revoke.disabled = false;
  }
}

passwordForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (password.value !== repeat.value) {
    pluginFeedback("Пароли не совпадают.");
    repeat.focus();
    return;
  }
  changePlugin("password", { password: password.value }, "Пароль сохранён.");
});
function showPluginStep(step) {
  pluginStep = step;
  pluginFeedback("");
  renderPlugin();
}
for (const button of document.querySelectorAll("[data-plugin-step]"))
  button.addEventListener("click", () => showPluginStep(button.dataset.pluginStep));
byId("continue-plugin").addEventListener("click", () => {
  showPluginStep("connect");
  byId("plugin-connect-heading").focus();
});
byId("edit-plugin-password").addEventListener("click", () => {
  editingPassword = true;
  renderPlugin();
  password.focus();
});
byId("cancel-plugin-password").addEventListener("click", () => {
  passwordForm.reset();
  editingPassword = false;
  pluginFeedback("");
  renderPlugin();
  byId("edit-plugin-password").focus();
});
revoke.addEventListener("click", () => {
  revokeDialog.returnValue = "cancel";
  revokeDialog.showModal();
});
revokeDialog.addEventListener("close", () => {
  if (revokeDialog.returnValue === "revoke")
    changePlugin("revoke", {}, "Доступ отозван. Для поиска подключитесь заново.");
});
pluginCheck.addEventListener("click", async () => {
  setText("endpoint-feedback", "");
  if (!await checkPlugin()) return;
  if (pluginState?.sessions) {
    showPluginStep("try");
    pluginFeedback("Доступ выдан. Можно искать в ChatGPT.");
    byId("plugin-try-heading").focus();
  } else if (pluginState?.running && !pluginState.local) {
    setText("endpoint-feedback", "Сервер доступен. Ожидаем вход из ChatGPT.");
  } else if (pluginState?.running) {
    setText("endpoint-feedback", "Локальная проверка прошла.");
  }
});
byId("copy-endpoint").addEventListener("click", () => copyText(endpoint.value, byId("endpoint-feedback")));

const form = byId("import");
const file = byId("file");
const dropzone = byId("dropzone");
const importTarget = byId("target");
const importName = byId("name");
const importButton = form.querySelector('button[type="submit"]');
const exportGuide = byId("export-guide");
let importMode = importTarget.value ? "existing" : "new";
let suggestedName = "";
let importing = false;
let uploadRequested = !isAdmin && document.body.dataset.view === "manual";

function titleFromFilename(filename) {
  const title = filename.normalize("NFC").replace(/\.(zip|txt)$/i, "")
    .replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, "")
    .replace(/^(?:whatsapp\s+chat\s+with|чат\s+whatsapp\s+с)\s+|^(?:whatsapp(?:\s+chat)?|чат\s+whatsapp|whatsapp\s+чат)\s*[-–—_:]\s*/i, "")
    .trim();
  return title.length <= 120 && !/^(?:_?chat|whatsapp(?: chat)?|чат(?: whatsapp)?|переписка)$/i.test(title) ? title : "";
}

function importFields() {
  const existing = importMode === "existing";
  byId("import-mode").value = importMode;
  byId("target-field").hidden = !existing;
  byId("name-field").hidden = existing;
  importTarget.disabled = !existing;
  importTarget.required = existing;
  importName.disabled = existing;
  importName.required = !existing;
  byId("mode-new").setAttribute("aria-pressed", String(!existing));
  byId("mode-existing").setAttribute("aria-pressed", String(existing));
  importButton.disabled = importing || !file.files.length || !file.validity.valid;
}

function selectFile() {
  const selected = file.files[0];
  const isText = /\.txt$/i.test(selected?.name || "");
  const maxSize = Number(isText ? file.dataset.maxText : file.dataset.maxZip);
  const limit = isText ? file.dataset.maxTextLabel : file.dataset.maxZipLabel;
  const error = selected && !/\.(zip|txt)$/i.test(selected.name) ? "Выберите TXT или ZIP из WhatsApp."
    : selected?.size > maxSize ? `Выберите файл до ${limit}.`
    : selected?.size === 0 ? "Файл пуст." : "";
  file.setCustomValidity(error);
  setText("import-file-error", error);
  byId("import-file-error").hidden = !error;
  setText("file-label", selected ? "Заменить файл" : "Выберите файл WhatsApp");
  setText("file-info", selected ? selected.name
    : `ZIP до ${file.dataset.maxZipLabel} · TXT до ${file.dataset.maxTextLabel}`);
  if (selected && !error) {
    const name = titleFromFilename(selected.name);
    if (!importName.value || importName.value === suggestedName) importName.value = name;
    suggestedName = name;
  }
  dropzone.toggleAttribute("data-selected", Boolean(selected && !error));
  byId("import-fields").hidden = !selected || Boolean(error);
  importFields();
}

for (const mode of ["new", "existing"]) {
  byId("mode-" + mode).addEventListener("click", () => {
    importMode = mode;
    importFields();
  });
}
file.addEventListener("change", selectFile);
dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.toggleAttribute("data-dragging", true);
});
dropzone.addEventListener("dragleave", () => dropzone.removeAttribute("data-dragging"));
dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.removeAttribute("data-dragging");
  const incoming = event.dataTransfer.files;
  if (incoming.length !== 1) {
    setText("import-file-error", "Выберите один чат за раз.");
    byId("import-file-error").hidden = false;
    return;
  }
  file.files = incoming;
  selectFile();
});

byId("open-export-guide").addEventListener("click", () => exportGuide.showModal());
exportGuide.querySelector("button[value=choose]").addEventListener("click", (event) => {
  event.preventDefault();
  exportGuide.close();
  file.click();
});
const guideChoices = exportGuide.querySelectorAll("[data-guide-os]");
for (const choice of guideChoices) {
  choice.addEventListener("click", () => {
    for (const item of guideChoices) item.setAttribute("aria-pressed", String(item === choice));
    setText("guide-open-chat", choice.dataset.guideOs === "ios" ? "Чат → имя вверху" : "Чат → ⋮ → Ещё");
  });
}

form.addEventListener("submit", (event) => {
  if (!isAdmin) {
    event.preventDefault();
    if (!importing) importMemberChat();
    return;
  }
  importButton.disabled = true;
  byId("import-progress").hidden = false;
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    window.location.reload();
    return;
  }
  selectFile();
  byId("import-progress").hidden = true;
});
byId("home-import")?.addEventListener("click", (event) => {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  window.location.hash = "#manual";
  navigate();
  file.click();
});
byId("import-another")?.addEventListener("click", () => file.click());

function openProfile() {
  const profile = byId("profile-dialog");
  if (profile.open) return;
  profile.showModal();
  checkPlugin();
}

let refreshingSource = false;
let nextMemberPollAt = 0;

function renderMemberUpload() {
  const existing = !byId("archive-rows").hidden;
  const visible = importing || (connected() && (uploadRequested || !existing));
  byId("member-upload").hidden = !visible;
  byId("add-chat").hidden = !connected() || !existing;
  byId("add-chat").setAttribute("aria-expanded", String(visible));
}

async function refreshMemberSource(force = false) {
  if ((!force && Date.now() < nextMemberPollAt) || refreshingSource || pending) return;
  refreshingSource = true;
  try {
    const wasConnected = connected();
    const revision = (value) => JSON.stringify([value?.job, value?.matches || value?.match_states, value?.updates]);
    const previous = revision(status);
    status = await api("source");
    offline = false;
    if (wasConnected && !connected() && memberMatchTarget) {
      memberMatchState = "idle";
      memberMatchSource = "";
      memberMatchChats = [];
    }
    if (!archiveKey || watchMatchTarget || previous !== revision(status)) await loadMemberChats().catch(() => {});
  } catch {
    offline = true;
  } finally {
    const waiting = connected() && archive.some((chat) => ["waiting", "updating"].includes(chat.sync_status));
    const active = offline || job() || waiting || ["STARTING", "SCAN_QR_CODE"].includes(status?.connection);
    nextMemberPollAt = Date.now() + (active ? 2000 : 12000);
    refreshingSource = false;
    render();
  }
}

function uploadMemberArchive(progress, percent) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", form.action);
    request.setRequestHeader("Accept", "application/json");
    request.responseType = "json";
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) percent.value = 100 * event.loaded / event.total;
    };
    request.upload.onload = () => {
      percent.hidden = true;
      progress.textContent = "Файл загружен. Импортируем переписку и вложения…";
    };
    request.onload = () => resolve(request);
    request.onerror = () => reject(new Error("Связь прервалась. Проверьте подключение и повторите импорт."));
    request.send(new FormData(form));
  });
}

async function importMemberChat() {
  const responseError = byId("import-response-error");
  const progress = byId("import-progress");
  const percent = byId("import-upload-percent");
  importing = true;
  importButton.disabled = true;
  responseError.hidden = true;
  progress.textContent = "Загружаем файл…";
  progress.hidden = percent.hidden = false;
  percent.value = 0;
  try {
    const response = await uploadMemberArchive(progress, percent);
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    const result = response.response;
    if (response.status !== 200 || !result?.chat_id) {
      throw new Error(result?.error || "Файл не импортирован. Попробуйте ещё раз.");
    }
    form.reset();
    importMode = "new";
    suggestedName = "";
    selectFile();
    uploadRequested = false;
    watchMatchTarget = result.sync_status === "waiting" ? result.chat_id : "";
    byId("member-notice").hidden = false;
    setText("member-notice", "Архив добавлен.");
    try {
      await loadMemberChats();
    } catch {
      window.location.reload();
      return;
    }
    refreshMemberSource(true);
  } catch (error) {
    setText("import-response-error", error.message);
    responseError.hidden = false;
    responseError.scrollIntoView({ block: "nearest" });
  } finally {
    importing = false;
    progress.hidden = percent.hidden = true;
    importFields();
    renderMemberUpload();
  }
}

const reconnect = byId("reconnect-dialog");
byId("reconnect-source").addEventListener("click", () => {
  reconnect.returnValue = "cancel";
  reconnect.showModal();
});
reconnect.addEventListener("close", () => {
  if (reconnect.returnValue === "reconnect") submit("reconnect", { confirm: true });
});
byId("connect").addEventListener("click", () => submit("connect"));
byId("renew-qr").addEventListener("click", () => submit("renew-qr"));
qr.addEventListener("load", () => { qr.hidden = false; byId("pairing-placeholder").hidden = true; });
qr.addEventListener("error", () => {
  qr.hidden = true;
  byId("pairing-placeholder").hidden = false;
  setText("pairing-placeholder", "QR обновляется…");
});
if (isAdmin) {
  byId("archive-owner-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const url = new URL("/admin", window.location.origin);
    const owner = byId("archive-owner").value;
    if (owner !== "admin") url.searchParams.set("owner", owner);
    url.hash = view;
    window.location.assign(url);
  });
  byId("create-user").addEventListener("click", () => openUserForm());
  byId("cancel-user").addEventListener("click", () => byId("user-dialog").close());
  byId("user-dialog").addEventListener("cancel", (event) => { if (userBusy) event.preventDefault(); });
  byId("user-dialog").addEventListener("close", () => byId("user-form").reset());
  byId("user-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const password = byId("user-password").value;
    changeUser(userAction ? { action: "password", username: userAction.username, password } :
      { action: "create", username: byId("user-login").value, name: byId("user-name").value, password },
      userAction ? "Пароль обновлён." : "Пользователь создан.");
  });
  byId("disable-user-dialog").addEventListener("close", (event) => {
    if (event.target.returnValue === "disable")
      changeUser({ action: "disable", username: userToDisable }, "Доступ отключён.");
  });
  byId("history-chat").addEventListener("change", () => loadHistory());
  byId("history-search").addEventListener("submit", (event) => {
    event.preventDefault();
    loadHistory();
  });
  byId("history-more").addEventListener("click", () => loadHistory(true));
  targetSelect.addEventListener("change", () => { target = targetSelect.value; renderControls(); });
  filter.addEventListener("input", renderCatalog);
  groupSelect.addEventListener("change", renderControls);
  byId("refresh-chats").addEventListener("click", loadCatalog);
  byId("refresh-groups").addEventListener("click", loadCatalog);
  byId("update-now").addEventListener("click", () => submit("updates/run"));
  byId("group-sync").addEventListener("click", () => submit("updates/group", { source: groupSelect.value }));
  byId("link-chat").addEventListener("click", async () => {
    if (await submit(correcting ? "updates/replace" : "updates/link", { target, source })) {
      correcting = "";
      await loadArchive();
      render();
    }
  });
  byId("cancel-mapping").addEventListener("click", () => {
    correcting = "";
    source = "";
    renderCatalog();
    render();
  });
} else {
  byId("open-profile").addEventListener("click", openProfile);
  byId("open-source").addEventListener("click", () => {
    byId("profile-dialog").close();
    window.location.hash = "#chats";
    navigateMember();
    refreshMemberSource(true);
    byId(connected() ? "reconnect-source" : "member-source-title").focus();
  });
  byId("member-match-filter").addEventListener("input", renderMemberMatchList);
  byId("member-match-refresh").addEventListener("click", loadMemberMatch);
  byId("member-match-submit").addEventListener("click", saveMemberMatch);
  byId("member-match-close").addEventListener("click", closeMemberMatch);
  byId("add-chat").addEventListener("click", () => {
    if (importing) return;
    uploadRequested = !uploadRequested;
    renderMemberUpload();
    if (uploadRequested) byId("member-upload-title").focus();
  });
  conversationViewport.addEventListener("scroll", loadEarlierHistory, { passive: true });
  window.addEventListener("online", () => {
    historyRetryBlocked = false;
    loadEarlierHistory();
  });
}
const pageNavigation = isAdmin ? navigate : navigateMember;
window.addEventListener("hashchange", pageNavigation);
selectFile();
pageNavigation();

async function poll() {
  try {
    const working = job();
    const revision = (value) => JSON.stringify([
      value?.matches || value?.match_states,
      value?.updates?.map((item) => [item.last?.at, item.enabled, item.error, item.source?.id]),
    ]);
    const seen = revision(status);
    const nextStatus = await api("source");
    if (isAdmin && !nextStatus.admin) {
      window.location.replace("/admin");
      return;
    }
    if (isAdmin && status && status.connection !== nextStatus.connection && nextStatus.connection !== "WORKING") {
      catalogState = "idle";
      catalog = [];
      source = "";
      renderCatalog();
    }
    status = nextStatus;
    offline = false;
    if (!archiveKey || (working && !job()) || seen !== revision(status)
        || (view !== "manual" && Date.now() - loadedAt > 60000)) await loadArchive();
  } catch {
    offline = true;
  } finally {
    render();
    window.setTimeout(poll, 2500);
  }
}
if (isAdmin) poll();
else {
  refreshMemberSource(true);
  window.setInterval(() => refreshMemberSource(), 2500);
  window.setInterval(() => loadMemberChats().catch(() => {}), 60000);
}
