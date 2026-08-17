(function (root) {
  "use strict";

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderInline(text) {
    const source = String(text);
    const patterns = [
      { type: "code", re: /`([^`]+)`/ },
      { type: "link", re: /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/ },
      { type: "bold", re: /\*\*([^*]+)\*\*/ },
      { type: "bold", re: /__([^_]+)__/ },
      { type: "italic", re: /\*([^*]+)\*/ },
      { type: "italic", re: /_([^_]+)_/ },
    ];
    let result = "";
    let index = 0;
    while (index < source.length) {
      let next = null;
      for (const pattern of patterns) {
        pattern.re.lastIndex = 0;
        const slice = source.slice(index);
        const match = pattern.re.exec(slice);
        if (!match) {
          continue;
        }
        const at = index + match.index;
        if (!next || at < next.at) {
          next = { type: pattern.type, at, match };
        }
      }
      if (!next) {
        result += escapeHtml(source.slice(index));
        break;
      }
      result += escapeHtml(source.slice(index, next.at));
      if (next.type === "code") {
        result += `<code>${escapeHtml(next.match[1])}</code>`;
      } else if (next.type === "link") {
        const href = escapeHtml(next.match[2]);
        result += `<a href="${href}" target="_blank" rel="noopener noreferrer">${escapeHtml(next.match[1])}</a>`;
      } else if (next.type === "bold") {
        result += `<strong>${renderInline(next.match[1])}</strong>`;
      } else {
        result += `<em>${renderInline(next.match[1])}</em>`;
      }
      index = next.at + next.match[0].length;
    }
    return result;
  }

  function flushParagraph(parts, lines) {
    if (!lines.length) {
      return;
    }
    parts.push(`<p>${renderInline(lines.join(" "))}</p>`);
    lines.length = 0;
  }

  function renderMarkdown(markdown) {
    const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    const parts = [];
    const paragraph = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];

      if (/^```/.test(line)) {
        flushParagraph(parts, paragraph);
        const language = escapeHtml(line.slice(3).trim());
        const code = [];
        index += 1;
        while (index < lines.length && !/^```/.test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        parts.push(
          `<pre class="md-code"><code${language ? ` class="language-${language}"` : ""}>${escapeHtml(code.join("\n"))}</code></pre>`,
        );
        index += 1;
        continue;
      }

      if (/^\s*$/.test(line)) {
        flushParagraph(parts, paragraph);
        index += 1;
        continue;
      }

      const heading = /^(#{1,6})\s+(.+?)\s*$/.exec(line);
      if (heading) {
        flushParagraph(parts, paragraph);
        const level = heading[1].length;
        parts.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flushParagraph(parts, paragraph);
        parts.push("<hr>");
        index += 1;
        continue;
      }

      if (/^>\s?/.test(line)) {
        flushParagraph(parts, paragraph);
        const quote = [];
        while (index < lines.length && /^>\s?/.test(lines[index])) {
          quote.push(lines[index].replace(/^>\s?/, ""));
          index += 1;
        }
        parts.push(`<blockquote>${renderMarkdown(quote.join("\n"))}</blockquote>`);
        continue;
      }

      const unordered = /^(\s*)[-*+]\s+(.*)$/.exec(line);
      const ordered = /^(\s*)\d+\.\s+(.*)$/.exec(line);
      if (unordered || ordered) {
        flushParagraph(parts, paragraph);
        const isOrdered = Boolean(ordered);
        const items = [];
        const itemRe = isOrdered ? /^(\s*)\d+\.\s+(.*)$/ : /^(\s*)[-*+]\s+(.*)$/;
        while (index < lines.length) {
          const item = itemRe.exec(lines[index]);
          if (!item) {
            break;
          }
          const body = item[2];
          const task = /^\[([ xX])\]\s+(.*)$/.exec(body);
          if (task) {
            const checked = task[1].toLowerCase() === "x" ? " checked" : "";
            items.push(
              `<li class="task-list-item"><input type="checkbox" disabled${checked}> ${renderInline(task[2])}</li>`,
            );
          } else {
            items.push(`<li>${renderInline(body)}</li>`);
          }
          index += 1;
        }
        parts.push(isOrdered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
        continue;
      }

      paragraph.push(line.trim());
      index += 1;
    }

    flushParagraph(parts, paragraph);
    return parts.join("\n") || "<p></p>";
  }

  root.renderMarkdown = renderMarkdown;
})(window.KanbanMarkdown = window.KanbanMarkdown || {});
