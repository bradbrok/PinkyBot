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
    const codeBlocks = [];
    let processed = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
        const idx = codeBlocks.length;
        const label = lang ? `<span class="lang-label">${lang}</span>` : '';
        codeBlocks.push(`<pre>${label}<code>${escapeHtml(code.trim())}</code></pre>`);
        return `\x00CB${idx}\x00`;
    });

    const inlineCodes = [];
    processed = processed.replace(/`([^`]+)`/g, (_, code) => {
        const idx = inlineCodes.length;
        inlineCodes.push(`<code>${escapeHtml(code)}</code>`);
        return `\x00IC${idx}\x00`;
    });

    processed = escapeHtml(processed);

    processed = processed.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    processed = processed.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    processed = processed.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    processed = processed.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    processed = processed.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    processed = processed.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
    processed = processed.replace(/^- (.+)$/gm, '<li>$1</li>');
    processed = processed.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');
    processed = processed.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');

    processed = processed.replace(
        /((?:^\|.+\|$\n?)+)/gm,
        (tableBlock) => {
            const rows = tableBlock.trim().split('\n').filter(r => r.trim());
            if (rows.length < 2) return tableBlock;
            const isSep = /^\|[\s\-:]+\|/.test(rows[1]);
            if (!isSep) return tableBlock;
            const parseRow = (row) => row.split('|').slice(1, -1).map(c => c.trim());
            const headers = parseRow(rows[0]);
            const dataRows = rows.slice(2);
            let html = '<table><thead><tr>';
            for (const h of headers) html += `<th>${h}</th>`;
            html += '</tr></thead><tbody>';
            for (const row of dataRows) {
                const cells = parseRow(row);
                html += '<tr>';
                for (const c of cells) html += `<td>${c}</td>`;
                html += '</tr>';
            }
            html += '</tbody></table>';
            return html;
        }
    );

    processed = processed.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    processed = processed.replace(/\n\n/g, '</p><p>');
    processed = processed.replace(/\n/g, '<br>');
    processed = `<p>${processed}</p>`;
    processed = processed.replace(/<p>\s*<(h[1-3]|pre|ul|ol|blockquote)/g, '<$1');
    processed = processed.replace(/<\/(h[1-3]|pre|ul|ol|blockquote)>\s*<\/p>/g, '</$1>');
    processed = processed.replace(/<p>\s*<\/p>/g, '');
    processed = processed.replace(/<p><br>/g, '<p>');
    processed = processed.replace(/<br><\/p>/g, '</p>');

    for (let i = 0; i < codeBlocks.length; i++) {
        processed = processed.replace(`\x00CB${i}\x00`, codeBlocks[i]);
    }
    for (let i = 0; i < inlineCodes.length; i++) {
        processed = processed.replace(`\x00IC${i}\x00`, inlineCodes[i]);
    }

    return processed;
}
