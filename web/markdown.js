export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderInline(text) {
  return text
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img class="inline-image" src="$2" alt="$1">')
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

/** 支持标题、引用、有序/无序列表、围栏代码块、表格和加粗。 */
export function renderMarkdown(value) {
  const html = [];
  const lines = escapeHtml(value).split("\n");
  let list = null;
  let index = 0;

  const closeList = () => {
    if (list) {
      html.push(`</${list}>`);
      list = null;
    }
  };
  const openList = (kind) => {
    if (list !== kind) {
      closeList();
      html.push(`<${kind}>`);
      list = kind;
    }
  };

  while (index < lines.length) {
    const raw = lines[index];
    const line = raw.trim();

    if (line.startsWith("```")) {
      closeList();
      const buffer = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        buffer.push(lines[index]);
        index += 1;
      }
      index += 1;
      html.push(`<pre><code>${buffer.join("\n")}</code></pre>`);
      continue;
    }

    if (!line) {
      closeList();
      index += 1;
      continue;
    }

    if (line.startsWith("|") && line.endsWith("|")) {
      closeList();
      const rows = [];
      while (index < lines.length) {
        const row = lines[index].trim();
        if (!row.startsWith("|") || !row.endsWith("|")) break;
        rows.push(row.slice(1, -1).split("|").map((cell) => cell.trim()));
        index += 1;
      }
      const isDivider = (cells) => cells.every((c) => /^:?-{2,}:?$/.test(c));
      const body = rows.filter((cells) => !isDivider(cells));
      if (body.length) {
        const [head, ...rest] = body;
        html.push("<table><thead><tr>");
        head.forEach((cell) => html.push(`<th>${renderInline(cell)}</th>`));
        html.push("</tr></thead><tbody>");
        rest.forEach((cells) => {
          html.push("<tr>");
          cells.forEach((cell) => html.push(`<td>${renderInline(cell)}</td>`));
          html.push("</tr>");
        });
        html.push("</tbody></table>");
      }
      continue;
    }

    // Discord 的小号灰字语法。来源说明用它，避免喧宾夺主。
    if (line.startsWith("-# ")) {
      closeList();
      html.push(`<p class="subtext">${renderInline(line.slice(3))}</p>`);
      index += 1;
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = Math.min(heading[1].length + 2, 6);
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (line.startsWith("&gt;")) {
      closeList();
      html.push(`<blockquote>${renderInline(line.replace(/^&gt;\s?/, ""))}</blockquote>`);
      index += 1;
      continue;
    }

    const bullet = line.match(/^[-*]\s+(.*)$/);
    if (bullet) {
      openList("ul");
      html.push(`<li>${renderInline(bullet[1])}</li>`);
      index += 1;
      continue;
    }

    const numbered = line.match(/^\d+[.、)]\s+(.*)$/);
    if (numbered) {
      openList("ol");
      html.push(`<li>${renderInline(numbered[1])}</li>`);
      index += 1;
      continue;
    }

    closeList();
    html.push(`<p>${renderInline(line)}</p>`);
    index += 1;
  }

  closeList();
  return html.join("");
}
