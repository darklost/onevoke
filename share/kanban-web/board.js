(function () {
  "use strict";

  const config = window.KANBAN_WEB || {};
  const ACTIVE_STATES = ["backlog", "todo", "working", "review", "done"];
  const ALL_STATES = ACTIVE_STATES.concat(["archived", "trash"]);

  const boardEl = document.getElementById("board");
  const errorEl = document.getElementById("board-error");
  const errorDetailEl = document.getElementById("board-error-detail");
  const keywordEl = document.getElementById("keyword");
  const toggleArchivedEl = document.getElementById("toggle-archived");
  const refreshStatusEl = document.getElementById("refresh-status");
  const retryEl = document.getElementById("retry");
  const dialogEl = document.getElementById("task-dialog");
  const dialogTitleEl = document.getElementById("task-dialog-title");
  const dialogMetaEl = document.getElementById("task-dialog-meta");
  const dialogBodyEl = document.getElementById("task-dialog-body");

  let tasks = [];
  let showArchived = false;
  let eventSource = null;
  const cards = new Map();
  const columns = new Map();

  function statusLabel(state) {
    return (config.statusLabels && config.statusLabels[state]) || state;
  }

  function sizeLabel(kind) {
    return (config.sizeLabels && config.sizeLabels[kind]) || kind;
  }

  function cardCount(count) {
    return String(config.cardCountLabel || "{count}").replace("{count}", String(count));
  }

  function visibleStates() {
    return showArchived ? ALL_STATES : ACTIVE_STATES;
  }

  function filteredTasks() {
    const keyword = (keywordEl.value || "").trim().toLowerCase();
    return tasks.filter((task) => {
      if (!showArchived && (task.state === "archived" || task.state === "trash")) {
        return false;
      }
      if (!keyword) {
        return true;
      }
      const haystack = [
        task.title,
        task.task_id,
        task.task_group,
        task.type,
        task.assignee,
        task.state,
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(keyword);
    });
  }

  function makeElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    if (text !== undefined) {
      element.textContent = text;
    }
    return element;
  }

  function createCard(task) {
    const card = makeElement("button", "task-card");
    card.type = "button";
    card.dataset.taskId = task.task_id;
    card.append(
      makeElement("p", "task-title"),
      makeElement("p", "task-id"),
      makeElement("span", "badge task-group"),
    );

    const meta = makeElement("div", "task-meta");
    const badges = makeElement("div", "task-badges");
    badges.append(
      makeElement("span", "badge type"),
      makeElement("span", "badge task-size"),
      makeElement("span", "badge task-state"),
    );
    meta.append(badges, makeElement("span", "task-assignee"));
    card.append(meta);

    const footer = makeElement("div", "task-footer");
    footer.append(makeElement("span", "task-time"));
    card.append(footer);
    updateCard(card, task);
    return card;
  }

  function updateCard(card, task) {
    card.dataset.state = task.state;
    card.querySelector(".task-title").textContent = task.title;
    const taskGroup = card.querySelector(".task-group");
    taskGroup.textContent = task.task_group || "";
    taskGroup.hidden = !task.task_group;
    card.querySelector(".task-id").textContent = task.task_id;
    card.querySelector(".badge.type").textContent = task.type || "-";

    const sizeBadge = card.querySelector(".task-size");
    sizeBadge.className = `badge task-size ${task.kind === "large" ? "large" : "secondary"}`;
    sizeBadge.textContent = sizeLabel(task.kind);

    const stateBadge = card.querySelector(".task-state");
    const showState = ["working", "review", "done", "archived", "trash"].includes(task.state);
    stateBadge.className = `badge task-state ${task.state}`;
    stateBadge.textContent = statusLabel(task.state);
    stateBadge.hidden = !showState;

    card.querySelector(".task-assignee").textContent =
      task.assignee || config.unassignedLabel || "";
    card.querySelector(".task-time").textContent = task.time || "-";
  }

  function ensureBoardStructure() {
    for (const state of ALL_STATES) {
      const section = makeElement("section", "column");
      section.dataset.testid = `task-column-${state}`;
      section.dataset.state = state;

      const header = makeElement("div", "column-header");
      header.append(
        makeElement("h2", "", statusLabel(state)),
        makeElement("span", "column-count", cardCount(0)),
      );

      const body = makeElement("div", "column-body");
      const empty = makeElement("p", "column-empty", config.emptyLabel || "");
      body.append(empty);
      section.append(header, body);
      boardEl.append(section);
      columns.set(state, {
        section,
        body,
        count: header.querySelector(".column-count"),
        empty,
      });
    }
  }

  function orderColumnCards(state, orderedCards) {
    const { body, empty } = columns.get(state);
    let cursor = body.firstElementChild;
    for (const card of orderedCards) {
      if (card === cursor) {
        cursor = cursor.nextElementSibling;
        continue;
      }
      body.insertBefore(card, cursor || empty);
    }
  }

  function patchBoard(nextTasks) {
    const nextIds = new Set(nextTasks.map((task) => task.task_id));
    for (const [taskId, card] of cards) {
      if (!nextIds.has(taskId)) {
        card.remove();
        cards.delete(taskId);
      }
    }

    const grouped = Object.fromEntries(ALL_STATES.map((state) => [state, []]));
    for (const task of nextTasks) {
      const column = columns.get(task.state);
      if (!column) {
        continue;
      }
      let card = cards.get(task.task_id);
      if (!card) {
        card = createCard(task);
        cards.set(task.task_id, card);
      } else {
        updateCard(card, task);
      }
      if (card.parentElement !== column.body) {
        column.body.insertBefore(card, column.empty);
      }
      grouped[task.state].push(card);
    }
    for (const state of ALL_STATES) {
      orderColumnCards(state, grouped[state]);
    }
    tasks = nextTasks;
    applyFilters();
  }

  function applyFilters() {
    const visibleIds = new Set(filteredTasks().map((task) => task.task_id));
    const counts = Object.fromEntries(ALL_STATES.map((state) => [state, 0]));
    for (const task of tasks) {
      const card = cards.get(task.task_id);
      if (!card) {
        continue;
      }
      card.hidden = !visibleIds.has(task.task_id);
      if (!card.hidden) {
        counts[task.state] += 1;
      }
    }
    for (const state of ALL_STATES) {
      const column = columns.get(state);
      const stateVisible = showArchived || !["archived", "trash"].includes(state);
      column.section.hidden = !stateVisible;
      column.count.textContent = cardCount(counts[state]);
      column.empty.hidden = counts[state] !== 0;
    }
    boardEl.dataset.columns = String(visibleStates().length);
  }

  function setError(detail) {
    boardEl.hidden = true;
    errorEl.hidden = false;
    if (detail !== undefined && detail !== null && detail !== "") {
      console.error(detail);
    }
    errorDetailEl.textContent = config.errorLabel || "";
    boardEl.setAttribute("aria-busy", "false");
  }

  function clearError() {
    errorEl.hidden = true;
    boardEl.hidden = false;
  }

  async function loadBoard() {
    boardEl.setAttribute("aria-busy", "true");
    try {
      const response = await fetch("/api/board", { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const payload = await response.json();
      applyBoardPayload(payload);
    } catch (error) {
      setError(error);
    } finally {
      boardEl.setAttribute("aria-busy", "false");
    }
  }

  function applyBoardPayload(payload) {
    const nextTasks = Array.isArray(payload.tasks) ? payload.tasks : [];
    patchBoard(nextTasks);
    clearError();
    const stamp = payload.generated_at || new Date().toISOString();
    refreshStatusEl.textContent = `${config.updatedLabel || "updated"} ${stamp}`;
    boardEl.setAttribute("aria-busy", "false");
  }

  async function openTask(taskId) {
    const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const task = await response.json();
    dialogTitleEl.textContent = task.title || task.task_id;
    dialogMetaEl.textContent = [
      task.task_id,
      statusLabel(task.state),
      sizeLabel(task.kind),
      task.type,
      task.assignee || config.unassignedLabel,
    ]
      .filter(Boolean)
      .join(" · ");
    dialogBodyEl.innerHTML = window.KanbanMarkdown.renderMarkdown(task.document || "");
    if (typeof dialogEl.showModal === "function") {
      dialogEl.showModal();
    }
  }

  function syncArchiveToggle() {
    toggleArchivedEl.setAttribute("aria-pressed", showArchived ? "true" : "false");
    toggleArchivedEl.textContent = showArchived
      ? config.showActiveLabel
      : config.showArchivedLabel;
  }

  function connectEvents() {
    if (!window.EventSource) {
      void loadBoard();
      return;
    }
    if (eventSource !== null) {
      eventSource.close();
    }
    eventSource = new EventSource("/api/events");
    eventSource.addEventListener("board", (event) => {
      try {
        applyBoardPayload(JSON.parse(event.data));
      } catch (error) {
        setError(error);
      }
    });
    eventSource.addEventListener("board-error", (event) => {
      try {
        const payload = JSON.parse(event.data);
        setError(payload.error || config.errorLabel || "");
      } catch (error) {
        setError(error);
      }
    });
    eventSource.onerror = () => {
      if (tasks.length === 0) {
        setError(config.errorLabel || "");
      }
    };
  }

  boardEl.addEventListener("click", (event) => {
    const target = event.target.closest("[data-task-id]");
    if (!target) {
      return;
    }
    void openTask(target.getAttribute("data-task-id")).catch((error) => {
      setError(error);
    });
  });

  keywordEl.addEventListener("input", () => {
    applyFilters();
  });

  toggleArchivedEl.addEventListener("click", () => {
    showArchived = !showArchived;
    syncArchiveToggle();
    applyFilters();
  });

  retryEl.addEventListener("click", () => {
    void loadBoard();
  });

  ensureBoardStructure();
  syncArchiveToggle();
  applyFilters();
  connectEvents();
})();
