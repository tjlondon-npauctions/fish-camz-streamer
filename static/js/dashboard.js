// Fish Camz Dashboard

function formatUptime(seconds) {
    if (!seconds || seconds <= 0) return '--';
    var d = Math.floor(seconds / 86400);
    var h = Math.floor((seconds % 86400) / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    if (m > 0) return m + 'm ' + s + 's';
    return s + 's';
}

function formatBitrate(kbps) {
    if (!kbps || kbps <= 0) return '--';
    if (kbps >= 1000) return (kbps / 1000).toFixed(1) + ' Mbps';
    return kbps.toFixed(0) + ' kbps';
}

// Toast notifications
function showToast(message, type) {
    type = type || 'info';
    var container = document.getElementById('toast-container');
    var toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.textContent = message;
    container.appendChild(toast);
    // Trigger animation
    setTimeout(function() { toast.classList.add('toast-visible'); }, 10);
    // Auto-dismiss after 4 seconds
    setTimeout(function() {
        toast.classList.remove('toast-visible');
        setTimeout(function() { container.removeChild(toast); }, 300);
    }, 4000);
}

// Stream status polling
function updateStreamStatus() {
    fetch('/api/status')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var indicator = document.getElementById('status-indicator');
            var status = document.getElementById('stream-status');
            var banner = document.getElementById('uptime-banner');
            var uptimeText = document.getElementById('uptime-text');

            if (data.running) {
                banner.className = 'status-banner status-online';
                indicator.className = 'indicator indicator-online';
                uptimeText.className = '';

                if (data.is_stalled) {
                    banner.className = 'status-banner status-warning';
                    indicator.className = 'indicator indicator-warning';
                    status.textContent = 'Stalled';
                } else if (data.is_slow) {
                    banner.className = 'status-banner status-warning';
                    indicator.className = 'indicator indicator-warning';
                    status.textContent = 'Slow (' + data.speed.toFixed(2) + 'x)';
                } else {
                    status.textContent = 'Live';
                }
            } else {
                banner.className = 'status-banner status-offline';
                indicator.className = 'indicator indicator-offline';
                status.textContent = data.last_error ? 'Error' : 'Offline';
                uptimeText.className = 'hidden';
            }

            document.getElementById('stream-uptime').textContent = formatUptime(data.uptime_seconds);
            document.getElementById('stream-restarts').textContent = data.restart_count || '0';
            document.getElementById('stream-fps').textContent = data.fps ? data.fps.toFixed(1) : '--';
            document.getElementById('stream-bitrate').textContent = formatBitrate(data.bitrate_kbps);
            document.getElementById('stream-speed').textContent = data.speed ? data.speed.toFixed(2) + 'x' : '--';
            document.getElementById('stream-frames').textContent = data.frame_count ? data.frame_count.toLocaleString() : '--';

            // Error banner
            var errorBanner = document.getElementById('error-banner');
            if (data.last_error && !data.running) {
                document.getElementById('error-text').textContent = data.last_error;
                errorBanner.className = '';
            } else {
                errorBanner.className = 'hidden';
            }
        })
        .catch(function() {});
}

function updateSystemStats() {
    fetch('/api/system')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            document.getElementById('sys-cpu').textContent = data.cpu_percent + '%';
            document.getElementById('sys-memory').textContent =
                data.memory.used_mb + ' / ' + data.memory.total_mb + ' MB (' + data.memory.percent + '%)';
            document.getElementById('sys-temp').textContent =
                data.temperature ? data.temperature + '\u00B0C' : 'N/A';
            document.getElementById('sys-disk').textContent =
                data.disk.free_gb + ' GB free (' + data.disk.percent + '% used)';
        })
        .catch(function() {});
}

function updateNetworkStatus() {
    fetch('/api/network')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var status = document.getElementById('net-status');
            if (data.connected) {
                status.textContent = 'Connected';
                status.className = 'text-success';
            } else {
                status.textContent = data.in_extended_outage ? 'Extended Outage' : 'Disconnected';
                status.className = 'text-error';
            }
            document.getElementById('net-latency').textContent =
                data.latency_ms ? data.latency_ms + ' ms' : '--';
        })
        .catch(function() {});
}

// Stream control with button feedback
function streamControl(action, btn) {
    // Disable all control buttons and show loading state
    var buttons = document.querySelectorAll('#btn-start, #btn-stop, #btn-restart');
    buttons.forEach(function(b) { b.disabled = true; });
    var originalText = btn.textContent;
    btn.setAttribute('aria-busy', 'true');
    btn.textContent = action === 'start' ? 'Starting...' : action === 'stop' ? 'Stopping...' : 'Restarting...';

    fetch('/api/stream/' + action, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                showToast('Failed to ' + action + ': ' + data.error, 'error');
            } else {
                showToast('Stream ' + action + ' command sent', 'success');
            }
        })
        .catch(function() {
            showToast('Failed to ' + action + ' stream — connection error', 'error');
        })
        .finally(function() {
            // Re-enable buttons after a delay to let the action take effect
            setTimeout(function() {
                buttons.forEach(function(b) { b.disabled = false; });
                btn.removeAttribute('aria-busy');
                btn.textContent = originalText;
                updateStreamStatus();
            }, 3000);
        });
}

// Version display
function loadVersion() {
    fetch('/api/version')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var el = document.getElementById('version-text');
            if (el && data.version) {
                el.textContent = 'v' + data.version;
            }
        })
        .catch(function() {});
}

// Initial load
updateStreamStatus();
updateSystemStats();
updateNetworkStatus();
loadVersion();

// Polling intervals
setInterval(updateStreamStatus, 3000);
setInterval(updateSystemStats, 10000);
setInterval(updateNetworkStatus, 15000);
