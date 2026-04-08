// --- Constants ---

export const TASK_STATUSES = ['pending', 'in_progress', 'blocked', 'completed'];

export const TASK_PRIORITIES = ['low', 'normal', 'high', 'urgent'];

export const RESEARCH_STATUSES = [
    { key: 'open', label: 'Open' },
    { key: 'assigned', label: 'Assigned' },
    { key: 'researching', label: 'Researching' },
    { key: 'in_review', label: 'In Review' },
    { key: 'published', label: 'Published' },
];

// --- Formatters ---

export function formatDate(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function formatDateTime(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function timeAgo(ts) {
    if (!ts) return '--';
    // Handle both unix timestamps and ISO strings
    const date = typeof ts === 'number' ? ts * 1000 : new Date(ts).getTime();
    const seconds = Math.floor((Date.now() - date) / 1000);
    if (seconds < 0) return 'just now';
    if (seconds < 60) return `${Math.floor(seconds)}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return new Date(date).toLocaleDateString();
}

export function truncate(str, len = 200) {
    if (!str || str.length <= len) return str;
    return str.substring(0, len) + '...';
}

export function statusColor(status) {
    if (status === 'online') return 'var(--green)';
    if (status === 'idle') return 'var(--yellow)';
    return 'var(--text-muted)';
}

export function statusLabel(status) {
    if (status === 'online') return 'working';
    if (status === 'idle') return 'idle';
    return 'offline';
}

export function contextClass(pct) {
    if (pct >= 80) return 'danger';
    if (pct >= 50) return 'warn';
    return '';
}

export function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

export function renderMarkdown(text) {
    const source = String(text || '').replace(/\r\n?/g, '\n');
    if (!source) return '';

    const codeBlocks = [];
    let processed = source.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
        const idx = codeBlocks.length;
        const label = lang ? `<span class="lang-label">${escapeHtml(lang)}</span>` : '';
        codeBlocks.push(`<pre>${label}<code>${escapeHtml(code.trim())}</code></pre>`);
        return `\u0000CB${idx}\u0000`;
    });

    const inlineCodes = [];
    processed = processed.replace(/`([^`\n]+)`/g, (_, code) => {
        const idx = inlineCodes.length;
        inlineCodes.push(`<code>${escapeHtml(code)}</code>`);
        return `\u0000IC${idx}\u0000`;
    });

    const applyInline = (value) => {
        let html = escapeHtml(value);
        html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
        // [[Wiki Link]] → clickable wiki cross-link
        html = html.replace(/\[\[([^\]]+)\]\]/g, (_, title) => {
            const slug = title.toLowerCase().replace(/\s+/g, '-');
            return `<a href="#" class="wiki-link" data-wiki-link="${escapeHtml(slug)}" data-wiki-title="${escapeHtml(title)}">${escapeHtml(title)}</a>`;
        });
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
        for (let i = 0; i < inlineCodes.length; i++) {
            html = html.replaceAll(`\u0000IC${i}\u0000`, inlineCodes[i]);
        }
        return html;
    };

    const parseTableRow = (row) => row.split('|').slice(1, -1).map((cell) => applyInline(cell.trim()));
    const isTableSeparator = (row) => /^\|(?:\s*:?-+:?\s*\|)+$/.test(row.trim());
    const lines = processed.split('\n');
    const blocks = [];
    let paragraph = [];
    let i = 0;

    const flushParagraph = () => {
        if (!paragraph.length) return;
        blocks.push(`<p>${paragraph.map((line) => applyInline(line)).join('<br>')}</p>`);
        paragraph = [];
    };

    while (i < lines.length) {
        const line = lines[i];
        const trimmed = line.trim();

        if (!trimmed) {
            flushParagraph();
            i += 1;
            continue;
        }

        if (/^\u0000CB\d+\u0000$/.test(trimmed)) {
            flushParagraph();
            blocks.push(trimmed);
            i += 1;
            continue;
        }

        const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
        if (heading) {
            flushParagraph();
            const level = heading[1].length;
            blocks.push(`<h${level}>${applyInline(heading[2])}</h${level}>`);
            i += 1;
            continue;
        }

        if (trimmed.startsWith('> ')) {
            flushParagraph();
            const quoteLines = [];
            while (i < lines.length && lines[i].trim().startsWith('> ')) {
                quoteLines.push(lines[i].trim().slice(2));
                i += 1;
            }
            blocks.push(`<blockquote>${quoteLines.map((entry) => applyInline(entry)).join('<br>')}</blockquote>`);
            continue;
        }

        if (trimmed.startsWith('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
            flushParagraph();
            const tableLines = [lines[i], lines[i + 1]];
            i += 2;
            while (i < lines.length && lines[i].trim().startsWith('|')) {
                tableLines.push(lines[i]);
                i += 1;
            }

            const headers = parseTableRow(tableLines[0]);
            const rows = tableLines.slice(2).map(parseTableRow);
            let tableHtml = '<table><thead><tr>';
            for (const headerCell of headers) tableHtml += `<th>${headerCell}</th>`;
            tableHtml += '</tr></thead><tbody>';
            for (const row of rows) {
                tableHtml += '<tr>';
                for (const cell of row) tableHtml += `<td>${cell}</td>`;
                tableHtml += '</tr>';
            }
            tableHtml += '</tbody></table>';
            blocks.push(tableHtml);
            continue;
        }

        const unorderedMatch = trimmed.match(/^- (.+)$/);
        const orderedMatch = trimmed.match(/^\d+\. (.+)$/);
        if (unorderedMatch || orderedMatch) {
            flushParagraph();
            const ordered = !!orderedMatch;
            const items = [];
            while (i < lines.length) {
                const listLine = lines[i].trim();
                const match = ordered ? listLine.match(/^\d+\. (.+)$/) : listLine.match(/^- (.+)$/);
                if (!match) break;
                items.push(applyInline(match[1]));
                i += 1;
            }
            const tag = ordered ? 'ol' : 'ul';
            blocks.push(`<${tag}>${items.map((item) => `<li>${item}</li>`).join('')}</${tag}>`);
            continue;
        }

        paragraph.push(line);
        i += 1;
    }

    flushParagraph();

    let html = blocks.join('');
    for (let i = 0; i < codeBlocks.length; i++) {
        html = html.replaceAll(`\u0000CB${i}\u0000`, codeBlocks[i]);
    }
    return html;
}
