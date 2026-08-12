const API = 'http://localhost:8000';
let pairsData = [];
let sortKey = 'score';
let sortDesc = true;
let charts = {};

async function startUpdate() {
    const btn = document.getElementById('updateBtn');
    btn.disabled = true;
    btn.textContent = '⏳ Updating...';
    try {
        const tf = document.getElementById('timeframe').value;
        const res = await fetch(`${API}/cache/update?timeframe=${tf}`, {method: 'POST'});
        const data = await res.json();
        console.log(data.message);
        await loadPairs(true);
    } catch (e) {
        alert('Update error: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '🔄 Update';
    }
}

async function loadPairs(force = false) {
    const btn = document.getElementById('loadBtn');
    const forceBtn = document.getElementById('forceBtn');
    btn.disabled = true;
    if (forceBtn) forceBtn.disabled = true;
    document.getElementById('loading').style.display = 'block';
    document.getElementById('pairsTable').style.display = 'none';

    const tf = document.getElementById('timeframe').value;
    const minCorr = document.getElementById('minCorr').value;
    const maxHL = document.getElementById('maxHL').value;
    const topN = document.getElementById('topN').value;

    const forceParam = force ? '&force_refresh=true' : '';
    const loadingText = force ? 'Fetching fresh data from Binance...' : 'Loading pairs...';
    document.querySelector('#loading p').textContent = loadingText;

    try {
        const res = await fetch(`${API}/pairs?timeframe=${tf}&min_correlation=${minCorr}&max_half_life=${maxHL}&top_n=${topN}${forceParam}`);
        const data = await res.json();
        pairsData = data.pairs || [];
        renderStats(data);
        renderTable();
    } catch (e) {
        alert('Error: ' + e.message);
    } finally {
        btn.disabled = false;
        if (forceBtn) forceBtn.disabled = false;
        document.getElementById('loading').style.display = 'none';
        document.getElementById('pairsTable').style.display = 'table';
    }
}

function renderStats(data) {
    const stats = document.getElementById('stats');
    const longs = pairsData.filter(p => p.signal === 'LONG').length;
    const shorts = pairsData.filter(p => p.signal === 'SHORT').length;
    const avgScore = pairsData.length > 0 ? (pairsData.reduce((a,b) => a + b.score, 0) / pairsData.length).toFixed(1) : 0;

    stats.innerHTML = `
        <div class="stat-card blue"><div class="label">Pairs Analyzed</div><div class="value">${data.total_analyzed || 0}</div></div>
        <div class="stat-card green"><div class="label">Passed</div><div class="value">${data.total_passed || 0}</div></div>
        <div class="stat-card green"><div class="label">Long Signals</div><div class="value">${longs}</div></div>
        <div class="stat-card red"><div class="label">Short Signals</div><div class="value">${shorts}</div></div>
        <div class="stat-card blue"><div class="label">Avg Score</div><div class="value">${avgScore}</div></div>
        <div class="stat-card"><div class="label">Symbols</div><div class="value">${data.symbols_count || 0}</div></div>
    `;
}

function getSignalClass(s) {
    return s === 'LONG' ? 'signal-long' : s === 'SHORT' ? 'signal-short' : s === 'CLOSE' ? 'signal-close' : 'signal-hold';
}

function getScoreClass(s) {
    return s >= 70 ? 'score-high' : s >= 50 ? 'score-mid' : 'score-low';
}

function getZBar(z) {
    const clamped = Math.max(-3, Math.min(3, z));
    const pct = ((clamped + 3) / 6) * 100;
    const color = z > 2 ? 'red' : z < -2 ? 'green' : 'gray';
    return `<div class="z-bar"><div class="z-indicator ${color}" style="left:${pct}%"></div></div>`;
}

function renderTable() {
    const tbody = document.getElementById('tableBody');
    tbody.innerHTML = '';

    const sorted = [...pairsData].sort((a, b) => {
        const av = a[sortKey] ?? 0;
        const bv = b[sortKey] ?? 0;
        return sortDesc ? (bv - av) : (av - bv);
    });

    sorted.forEach(p => {
        const row = document.createElement('tr');
        row.onclick = () => openPairDetail(p.symbol_a, p.symbol_b);
        row.innerHTML = `
            <td class="${getScoreClass(p.score)}">${p.score}</td>
            <td><strong>${p.pair}</strong></td>
            <td class="${getSignalClass(p.signal)}">${p.signal}</td>
            <td>${p.z_score > 0 ? '+' : ''}${p.z_score.toFixed(2)}${getZBar(p.z_score)}</td>
            <td>${p.correlation_6m.toFixed(3)}</td>
            <td style="color:${p.cointegration_pvalue < 0.05 ? '#3fb950' : '#f85149'}">${p.cointegration_pvalue.toFixed(4)}</td>
            <td style="color:${p.adf_pvalue < 0.05 ? '#3fb950' : '#f85149'}">${p.adf_pvalue.toFixed(4)}</td>
            <td>${p.half_life_days < 999 ? p.half_life_days.toFixed(1) : '∞'}</td>
            <td>${p.spread_deviation_pct > 0 ? '+' : ''}${p.spread_deviation_pct.toFixed(1)}%</td>
        `;
        tbody.appendChild(row);
    });
}

function sortBy(key) {
    if (sortKey === key) sortDesc = !sortDesc;
    else { sortKey = key; sortDesc = true; }
    renderTable();
}

async function openPairDetail(symA, symB) {
    const tf = document.getElementById('timeframe').value;
    document.getElementById('modal').classList.add('active');
    document.getElementById('modalTitle').textContent = `${symA} / ${symB}`;

    try {
        const res = await fetch(`${API}/pair/${symA}/${symB}?timeframe=${tf}`);
        const data = await res.json();

        document.getElementById('pairInfo').innerHTML = `
            <div class="info-item"><div class="label">Z-Score</div><div class="value" style="color:${data.z_score > 2 || data.z_score < -2 ? '#f85149' : '#58a6ff'}">${data.z_score.toFixed(2)}</div></div>
            <div class="info-item"><div class="label">Signal</div><div class="value ${getSignalClass(data.signal)}">${data.signal}</div></div>
            <div class="info-item"><div class="label">Score</div><div class="value ${getScoreClass(data.score)}">${data.score}</div></div>
            <div class="info-item"><div class="label">Corr 6M</div><div class="value">${data.correlation_6m.toFixed(3)}</div></div>
            <div class="info-item"><div class="label">Half-life</div><div class="value">${data.half_life_days.toFixed(1)}d</div></div>
            <div class="info-item"><div class="label">Spread Dev</div><div class="value">${data.spread_deviation_pct > 0 ? '+' : ''}${data.spread_deviation_pct.toFixed(1)}%</div></div>
            <div class="info-item"><div class="label">Coint p-val</div><div class="value" style="color:${data.cointegration_pvalue < 0.05 ? '#3fb950' : '#f85149'}">${data.cointegration_pvalue.toFixed(4)}</div></div>
            <div class="info-item"><div class="label">ADF p-val</div><div class="value" style="color:${data.adf_pvalue < 0.05 ? '#3fb950' : '#f85149'}">${data.adf_pvalue.toFixed(4)}</div></div>
        `;

        renderCharts(data);
    } catch (e) {
        alert('Error loading pair detail: ' + e.message);
    }
}

function closeModal() {
    document.getElementById('modal').classList.remove('active');
    Object.values(charts).forEach(c => c.destroy());
    charts = {};
}

function renderCharts(data) {
    const labels = data.dates || [];

    // Z-Score Chart
    const zCtx = document.getElementById('zScoreChart').getContext('2d');
    if (charts.z) charts.z.destroy();
    charts.z = new Chart(zCtx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                { label: 'Z-Score', data: data.z_score_history, borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,0.1)', fill: true, pointRadius: 0, tension: 0.3 },
                { label: '+2σ', data: data.upper_2, borderColor: '#f85149', borderDash: [5,5], pointRadius: 0 },
                { label: '-2σ', data: data.lower_2, borderColor: '#3fb950', borderDash: [5,5], pointRadius: 0 },
                { label: '0', data: data.zero, borderColor: '#8b949e', borderDash: [2,2], pointRadius: 0 }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#c9d1d9' } } },
            scales: {
                x: { ticks: { color: '#8b949e', maxTicksLimit: 8 }, grid: { color: '#21262d' } },
                y: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' } }
            }
        }
    });

    // Spread Deviation Chart
    const sCtx = document.getElementById('spreadChart').getContext('2d');
    if (charts.s) charts.s.destroy();
    charts.s = new Chart(sCtx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                { label: 'Spread Deviation %', data: data.spread_deviation_pct_history, borderColor: '#d29922', backgroundColor: 'rgba(210,153,34,0.1)', fill: true, pointRadius: 0, tension: 0.3 }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#c9d1d9' } } },
            scales: {
                x: { ticks: { color: '#8b949e', maxTicksLimit: 8 }, grid: { color: '#21262d' } },
                y: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' } }
            }
        }
    });
}

// Auto-load on startup
loadPairs();
