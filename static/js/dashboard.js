// Dashboard AJAX polling

function formatUptime(seconds) {
    if (!seconds || seconds <= 0) return '--';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return h + 'h ' + m + 'm';
    if (m > 0) return m + 'm ' + s + 's';
    return s + 's';
}

function formatBitrate(kbps) {
    if (!kbps || kbps <= 0) return '--';
    if (kbps >= 1000) return (kbps / 1000).toFixed(1) + ' Mbps';
    return kbps.toFixed(0) + ' kbps';
}

function updateStreamStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            const indicator = document.getElementById('status-indicator');
            const status = document.getElementById('stream-status');

            if (data.running) {
                indicator.className = 'indicator indicator-online';
                if (data.is_stalled) {
                    indicator.className = 'indicator indicator-warning';
                    status.textContent = 'Stalled';
                } else if (data.is_slow) {
                    indicator.className = 'indicator indicator-warning';
                    status.textContent = 'Slow (' + data.speed.toFixed(2) + 'x)';
                } else {
                    status.textContent = 'Streaming';
                }
            } else {
                indicator.className = 'indicator indicator-offline';
                status.textContent = data.last_error || 'Stopped';
            }

            document.getElementById('stream-uptime').textContent = formatUptime(data.uptime_seconds);
            document.getElementById('stream-restarts').textContent = data.restart_count || '0';
            document.getElementById('stream-fps').textContent = data.fps ? data.fps.toFixed(1) : '--';
            document.getElementById('stream-bitrate').textContent = formatBitrate(data.bitrate_kbps);
            document.getElementById('stream-speed').textContent = data.speed ? data.speed.toFixed(2) + 'x' : '--';
            document.getElementById('stream-frames').textContent = data.frame_count ? data.frame_count.toLocaleString() : '--';

            // Show/hide uptime banner
            var uptimeBanner = document.getElementById('uptime-banner');
            if (uptimeBanner) {
                uptimeBanner.className = '';
            }

            // Show error banner
            const banner = document.getElementById('error-banner');
            if (data.last_error && !data.running) {
                document.getElementById('error-text').textContent = data.last_error;
                banner.className = '';
            } else {
                banner.className = 'hidden';
            }
        })
        .catch(() => {});
}

function updateSystemStats() {
    fetch('/api/system')
        .then(r => r.json())
        .then(data => {
            document.getElementById('sys-cpu').textContent = data.cpu_percent + '%';
            document.getElementById('sys-memory').textContent =
                data.memory.used_mb + ' / ' + data.memory.total_mb + ' MB (' + data.memory.percent + '%)';
            document.getElementById('sys-temp').textContent =
                data.temperature ? data.temperature + '\u00B0C' : 'N/A';
            document.getElementById('sys-disk').textContent =
                data.disk.free_gb + ' GB free (' + data.disk.percent + '% used)';
        })
        .catch(() => {});
}

function updateNetworkStatus() {
    fetch('/api/network')
        .then(r => r.json())
        .then(data => {
            const status = document.getElementById('net-status');
            if (data.connected) {
                status.textContent = 'Connected';
                status.style.color = '#22c55e';
            } else {
                status.textContent = data.in_extended_outage ? 'Extended Outage' : 'Disconnected';
                status.style.color = '#ef4444';
            }
            document.getElementById('net-latency').textContent =
                data.latency_ms ? data.latency_ms + ' ms' : '--';
        })
        .catch(() => {});
}

function streamControl(action) {
    fetch('/api/stream/' + action, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                alert('Error: ' + data.error);
            }
            // Refresh status after a short delay
            setTimeout(updateStreamStatus, 2000);
        })
        .catch(() => {
            alert('Failed to ' + action + ' stream.');
        });
}

// Initial load
updateStreamStatus();
updateSystemStats();
updateNetworkStatus();

// Poll every 3 seconds
setInterval(updateStreamStatus, 3000);
setInterval(updateSystemStats, 10000);
setInterval(updateNetworkStatus, 15000);
