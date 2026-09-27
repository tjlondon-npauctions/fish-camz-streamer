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

// Last ping result, so the link verdict can tell "no internet" from
// "internet fine, but Bunny unreachable"
var pingConnected = null;

function updateNetworkStatus() {
    fetch('/api/network')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var status = document.getElementById('net-status');
            pingConnected = data.connected;
            if (data.connected) {
                status.textContent = 'Connected';
                status.className = 'text-success';
            } else {
                status.textContent = data.in_extended_outage ? 'Extended Outage' : 'Disconnected';
                status.className = 'text-error';
            }
            var latency = document.getElementById('net-latency');
            if (!data.connected) {
                latency.textContent = '--';
                latency.className = '';
                return;
            }
            var text = data.latency_ms ? data.latency_ms + ' ms' : '--';
            if (data.jitter_ms) text += ' \u00B1' + data.jitter_ms;
            text += ' \u00B7 ' + (data.loss_percent || 0) + '% loss';
            if (data.loss_percent_avg !== null && data.loss_percent_avg !== undefined) {
                text += ' (' + data.loss_percent_avg + '% over 5 min)';
            }
            latency.textContent = text;
            var loss = Math.max(data.loss_percent || 0, data.loss_percent_avg || 0);
            latency.className = loss >= 10 ? 'text-error' : loss > 0 ? 'text-warning' : '';
        })
        .catch(function() {});
}

function formatAge(seconds) {
    if (seconds === null || seconds === undefined) return 'never';
    seconds = Math.max(0, Math.round(seconds));
    if (seconds < 60) return seconds + 's ago';
    return formatUptime(seconds) + ' ago';
}

function formatBytes(bytes) {
    if (!bytes) return '0 MB';
    if (bytes >= 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
    return (bytes / 1e6).toFixed(0) + ' MB';
}

// Backlog big enough to be worth offering the skip button (~1 min of video)
var SKIP_BACKLOG_MIN_SEGMENTS = 10;

function updateUploader() {
    fetch('/api/uploader')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var section = document.getElementById('upload-section');
            updateLink(data.running ? data.link : null);
            if (!data.running && !data.upload_count) {
                section.className = 'hidden';  // RTMP-only Pi, or streamer stopped
                return;
            }
            section.className = '';

            // The single most useful signal: when viewers last got new video
            var playlist = document.getElementById('up-playlist');
            var age = data.playlist_age_seconds;
            playlist.textContent = formatAge(age);
            if (age === null || age === undefined || age > 300) {
                playlist.className = 'text-error';
            } else if (age > 60) {
                playlist.className = 'text-warning';
            } else {
                playlist.className = 'text-success';
            }

            var backlog = document.getElementById('up-backlog');
            var n = data.backlog_segments || 0;
            if (n === 0) {
                backlog.textContent = 'Nothing \u2014 up to date';
                backlog.className = 'text-success';
            } else {
                backlog.textContent = n + ' segments \u00B7 ' +
                    formatUptime(Math.round(data.backlog_behind_seconds)) + ' behind \u00B7 ' +
                    formatBytes(data.backlog_bytes);
                backlog.className = n >= SKIP_BACKLOG_MIN_SEGMENTS ? 'text-warning' : '';
            }

            var policy = document.getElementById('up-policy');
            var mins = data.catch_up_minutes;
            var text;
            if (mins === null || mins === undefined) text = 'Upload everything';
            else if (mins === 0) text = 'Live only';
            else text = 'Upload last ' + mins + ' min';
            if (data.backlog_skipped_count) {
                text += ' (' + data.backlog_skipped_count + ' older segments skipped)';
            }
            policy.textContent = text;

            var errors = document.getElementById('up-errors');
            errors.textContent = data.error_count
                ? data.error_count + (data.last_error ? ' \u2014 last: ' + data.last_error : '')
                : 'None';
            errors.className = data.error_count ? 'text-warning' : '';

            // Only offer the skip when there's something behind the live window
            // to drop — the playlist's own segments are never deleted
            var skippable = data.backlog_behind_live || 0;
            document.getElementById('btn-skip-backlog').className =
                'outline secondary' + (skippable >= SKIP_BACKLOG_MIN_SEGMENTS ? '' : ' hidden');
        })
        .catch(function() {});
}

var LINK_STATUS = {
    ok:          ['Good', 'text-success'],
    marginal:    ['Marginal', 'text-warning'],
    throttled:   ['Throttled', 'text-error'],
    down:        ['Down', 'text-error'],
    bunny_error: ['Bunny problem', 'text-warning'],
    bunny_auth:  ['Bunny key rejected', 'text-error'],
    unknown:     ['No recent uploads', '']
};

var FAILURE_LABELS = {
    offline: 'couldn\u2019t connect',
    slow: 'timed out',
    dropped: 'dropped',
    bunny: 'Bunny error',
    auth: 'key rejected',
    other: 'other'
};

function updateLink(link) {
    var status = document.getElementById('link-status');
    var speed = document.getElementById('link-speed');
    var failures = document.getElementById('link-failures');
    if (!link) {
        status.textContent = speed.textContent = failures.textContent = '--';
        return;
    }

    var s = LINK_STATUS[link.status] || [link.status, ''];
    var detail = link.detail;
    if (link.status === 'down' && pingConnected) {
        detail += ' (ping still works \u2014 suspect DNS or Bunny, not the boat\u2019s internet)';
    }
    status.textContent = s[0] + ' \u2014 ' + detail;
    status.className = s[1];

    if (link.upload_kbps) {
        var text = formatBitrate(link.upload_kbps);
        if (link.stream_kbps) {
            text += ' \u00B7 stream needs ' + formatBitrate(link.stream_kbps);
            if (link.headroom) text += ' (' + link.headroom.toFixed(1) + '\u00D7)';
        }
        speed.textContent = text;
        speed.className = link.headroom && link.headroom < 1 ? 'text-error'
            : link.headroom && link.headroom < 1.5 ? 'text-warning' : '';
    } else {
        speed.textContent = link.stream_kbps
            ? 'not measured yet \u00B7 stream needs ' + formatBitrate(link.stream_kbps)
            : 'not measured yet';
        speed.className = '';
    }

    var parts = [];
    Object.keys(link.failures || {}).forEach(function(kind) {
        parts.push(link.failures[kind] + ' ' + (FAILURE_LABELS[kind] || kind));
    });
    failures.textContent = parts.length
        ? parts.join(', ') + ' \u00B7 ' + link.uploads_ok + ' succeeded'
        : 'None (' + link.uploads_ok + ' succeeded)';
    failures.className = parts.length ? 'text-warning' : '';
}

function skipBacklog(btn) {
    if (!confirm('Delete all video waiting to upload, apart from the live edge? ' +
                 'That footage will be missing from the DVR.')) return;
    btn.disabled = true;
    fetch('/api/uploader/skip-backlog', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                showToast('Skip failed: ' + data.error, 'error');
            } else {
                showToast('Skipped ' + data.deleted + ' segments \u2014 uploading from live', 'success');
            }
        })
        .catch(function() {
            showToast('Skip failed \u2014 connection error', 'error');
        })
        .finally(function() {
            btn.disabled = false;
            updateUploader();
        });
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
updateUploader();
loadVersion();

// Polling intervals
setInterval(updateStreamStatus, 3000);
setInterval(updateSystemStats, 10000);
setInterval(updateNetworkStatus, 10000);
setInterval(updateUploader, 5000);
