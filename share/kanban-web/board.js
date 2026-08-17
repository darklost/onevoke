(function () {
  "use strict";

  const config = window.KANBAN_WEB || {};
  const ACTIVE_STATES = ["backlog", "todo", "working", "done"];
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
  let timer = null;

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

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
        task.type,
        task.assignee,
        task.state,
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(keyword);
    });
  }

  function renderCard(task) {
    const assignee = task.assignee || config.unassignedLabel || "";
    const badges = [
      `<span class="badge type">${escapeHtml(task.type || "-")}</span>`,
      `<span class="badge ${task.kind === "large" ? "large" : "secondary"}">${escapeHtml(sizeLabel(task.kind))}</span>`,
    ];
    if (task.state === "working" || task.state === "done" || task.state === "archived" || task.state === "trash") {
      badges.push(
        `<span class="badge ${escapeHtml(task.state)}">${escapeHtml(statusLabel(task.state))}</span>`,
      );
    }
    return `
      <button type="button" class="task-card" data-task-id="${escapeHtml(task.task_id)}">
        <p class="task-title">${escapeHtml(task.title)}</p>
        <p class="task-id">${escapeHtml(task.task_id)}</p>
        <div class="task-meta">
          <div class="task-badges">${badges.join("")}</div>
          <span class="task-assignee">${escapeHtml(assignee)}</span>
        </div>
        <div class="task-footer">
          <span class="task-time">${escapeHtml(task.time || "-")}</span>
        </div>
      </button>
    `;
  }

  function renderBoard() {
    const grouped = Object.fromEntries(ALL_STATES.map((state) => [state, []]));
    for (const task of filteredTasks()) {
      if (grouped[task.state]) {
        grouped[task.state].push(task);
      }
    }
    const states = visibleStates();
    boardEl.dataset.columns = String(states.length);
    boardEl.innerHTML = states
      .map((state) => {
        const items = grouped[state] || [];
        const body =
          items.length === 0
            ? `<p class="column-empty">${escapeHtml(config.emptyLabel || "")}</p>`
            : items.map(renderCard).join("");
        return `
          <section class="column" data-testid="task-column-${escapeHtml(state)}">
            <div class="column-header">
              <h2>${escapeHtml(statusLabel(state))}</h2>
              <span class="column-count">${escapeHtml(cardCount(items.length))}</span>
            </div>
            <div class="column-body">${body}</div>
          </section>
        `;
      })
      .join("");
  }

  function setError(message) {
    boardEl.hidden = true;
    errorEl.hidden = false;
    errorDetailEl.textContent = message || config.errorLabel || "";
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
      tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
      clearError();
      renderBoard();
      const stamp = payload.generated_at || new Date().toISOString();
      refreshStatusEl.textContent = `${config.updatedLabel || "updated"} ${stamp}`;
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      boardEl.setAttribute("aria-busy", "false");
    }
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

  function scheduleRefresh() {
    if (timer !== null) {
      window.clearInterval(timer);
    }
    const refreshMs = Number(config.refreshMs) || 5000;
    timer = window.setInterval(() => {
      void loadBoard();
    }, refreshMs);
  }

  boardEl.addEventListener("click", (event) => {
    const target = event.target.closest("[data-task-id]");
    if (!target) {
      return;
    }
    void openTask(target.getAttribute("data-task-id")).catch((error) => {
      setError(error instanceof Error ? error.message : String(error));
    });
  });

  keywordEl.addEventListener("input", () => {
    renderBoard();
  });

  toggleArchivedEl.addEventListener("click", () => {
    showArchived = !showArchived;
    syncArchiveToggle();
    renderBoard();
  });

  retryEl.addEventListener("click", () => {
    void loadBoard();
  });

  syncArchiveToggle();
  void loadBoard();
  scheduleRefresh();
})();
