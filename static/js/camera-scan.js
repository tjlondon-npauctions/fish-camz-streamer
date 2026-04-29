/**
 * Camera discovery, probing, and selection UI for setup and settings pages.
 *
 * Expects the host page to provide:
 *   #cam_username, #cam_password — credential inputs
 *   #rtsp_url                    — canonical RTSP URL input (saved on form submit)
 *   #scan-results                — container for device cards
 *   #selected-camera             — Selected card (toggled visible on selection)
 *   #selected-summary, #selected-url — populated by _renderSelectedCamera
 *   #probed_url, #probed_codec, #probed_resolution,
 *   #probed_framerate, #probed_can_copy — hidden inputs persisted on save
 *   setText(el, text)            — helper defined inline by the host page
 */

/* exported scanCameras, probeCamera, clearSelectedCamera */
/* global setText */

function _getCamCredentials() {
    var u = document.getElementById('cam_username');
    var p = document.getElementById('cam_password');
    return {
        username: u ? u.value : '',
        password: p ? p.value : '',
    };
}

function _setRtspUrl(url) {
    var inp = document.getElementById('rtsp_url');
    if (inp) inp.value = url;
}

/**
 * Render the "Selected" card and write the hidden probe fields so the
 * choice survives a page reload after save.
 */
function _renderSelectedCamera(url, probe) {
    _setRtspUrl(url);

    // Persist probe summary into hidden inputs for form submit
    var fields = {
        probed_url: url,
        probed_codec: probe.video_codec || '',
        probed_resolution: probe.resolution || '',
        probed_framerate: probe.framerate != null ? String(probe.framerate) : '',
        probed_can_copy: probe.can_copy ? '1' : '',
    };
    Object.keys(fields).forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.value = fields[id];
    });

    // Update the visible card
    var card = document.getElementById('selected-camera');
    if (!card) return;
    card.classList.remove('hidden');

    // Replace card body
    while (card.firstChild) card.removeChild(card.firstChild);

    var summary = document.createElement('strong');
    summary.className = 'selected-summary';
    var fps = probe.framerate ? Math.round(probe.framerate) : 0;
    summary.textContent = '✓ Selected: ' +
        (probe.video_codec || '?').toUpperCase() + ' ' +
        (probe.resolution || '') +
        (fps ? ' @ ' + fps + 'fps' : '');
    card.appendChild(summary);

    var note = document.createElement('small');
    note.textContent = probe.can_copy
        ? 'Codec passthrough OK (low CPU)'
        : 'Will transcode on the Pi (higher CPU)';
    card.appendChild(note);

    var code = document.createElement('code');
    code.id = 'selected-url';
    code.textContent = url;
    card.appendChild(code);

    var actions = document.createElement('div');
    actions.className = 'selected-actions';

    var retest = document.createElement('button');
    retest.type = 'button';
    retest.className = 'outline secondary';
    retest.textContent = 'Re-test';
    retest.addEventListener('click', function() { probeCamera(); });
    actions.appendChild(retest);

    var change = document.createElement('button');
    change.type = 'button';
    change.className = 'outline secondary';
    change.textContent = 'Change';
    change.addEventListener('click', function() { clearSelectedCamera(); });
    actions.appendChild(change);

    card.appendChild(actions);
}

/**
 * Hide the Selected card and re-open the manual RTSP fallback so the
 * user can pick a different camera.
 */
function clearSelectedCamera() {
    var card = document.getElementById('selected-camera');
    if (card) card.classList.add('hidden');
    _setRtspUrl('');

    var fallback = document.querySelector('details.cam-fallback');
    if (fallback) fallback.open = true;

    ['probed_url', 'probed_codec', 'probed_resolution', 'probed_framerate', 'probed_can_copy']
        .forEach(function(id) {
            var el = document.getElementById(id);
            if (el) el.value = '';
        });
}

/**
 * Probe whatever URL is currently in the rtsp_url input and, on success,
 * render the Selected card. Used by the "Test connection" / "Re-test" buttons.
 */
function probeCamera() {
    var url = document.getElementById('rtsp_url').value.trim();
    if (!url) {
        alert('Enter or scan for an RTSP URL first.');
        return;
    }

    var card = document.getElementById('selected-camera');
    if (card) {
        card.classList.remove('hidden');
        while (card.firstChild) card.removeChild(card.firstChild);
        var p = document.createElement('p');
        p.setAttribute('aria-busy', 'true');
        p.textContent = 'Testing camera connection...';
        card.appendChild(p);
    }

    fetch('/api/camera/probe?url=' + encodeURIComponent(url))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                _renderProbeError(card, data.error);
            } else {
                _renderSelectedCamera(url, data);
            }
        })
        .catch(function() {
            _renderProbeError(card, 'Connection test failed.');
        });
}

function _renderProbeError(card, message) {
    if (!card) {
        alert(message);
        return;
    }
    while (card.firstChild) card.removeChild(card.firstChild);
    var strong = document.createElement('strong');
    strong.textContent = '✗ ' + message;
    card.appendChild(strong);
    var hint = document.createElement('p');
    hint.innerHTML = '<small>Check the camera credentials above and try again, or pick a different camera.</small>';
    card.appendChild(hint);
    var actions = document.createElement('div');
    actions.className = 'selected-actions';
    var change = document.createElement('button');
    change.type = 'button';
    change.className = 'outline secondary';
    change.textContent = 'Change';
    change.addEventListener('click', function() { clearSelectedCamera(); });
    actions.appendChild(change);
    card.appendChild(actions);
}

/**
 * Scan the network for ONVIF cameras and render results as device cards.
 */
function scanCameras() {
    var div = document.getElementById('scan-results');
    div.className = '';
    while (div.firstChild) div.removeChild(div.firstChild);
    var loading = document.createElement('p');
    loading.setAttribute('aria-busy', 'true');
    loading.textContent = 'Scanning network for cameras...';
    div.appendChild(loading);

    fetch('/api/cameras/scan')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            while (div.firstChild) div.removeChild(div.firstChild);

            if (!data.cameras || data.cameras.length === 0) {
                setText(div, 'No cameras found. Use "Enter RTSP URL manually" below if you know the URL.');
                return;
            }

            var heading = document.createElement('p');
            heading.innerHTML = '<small>Found ' + data.cameras.length + ' device(s):</small>';
            div.appendChild(heading);

            data.cameras.forEach(function(cam) {
                div.appendChild(_renderDeviceCard(cam));
            });
        })
        .catch(function() {
            setText(div, 'Scan failed. Check network connectivity to the camera LAN.');
        });
}

/**
 * Render a single device card with one primary action.
 */
function _renderDeviceCard(cam) {
    var isNvr = cam.device_type === 'nvr';

    var card = document.createElement('div');
    card.className = 'cam-device';

    var header = document.createElement('div');
    header.className = 'cam-device-header';

    var meta = document.createElement('div');
    meta.className = 'cam-device-meta';
    var title = document.createElement('strong');
    title.textContent = (cam.name || 'Camera') + ' (' + cam.ip + ')';
    meta.appendChild(title);
    if (cam.hardware) {
        var hw = document.createElement('div');
        hw.innerHTML = '<small>' + cam.hardware + (isNvr ? ' · NVR' : '') + '</small>';
        meta.appendChild(hw);
    } else if (isNvr) {
        var nvrTag = document.createElement('div');
        nvrTag.innerHTML = '<small>NVR</small>';
        meta.appendChild(nvrTag);
    }
    header.appendChild(meta);

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'outline';
    btn.textContent = isNvr ? 'Choose channel' : 'Use this camera';
    btn.addEventListener('click', function() {
        if (isNvr) {
            _detectChannels(cam, card);
        } else {
            _quickProbeCamera(cam, card);
        }
    });
    header.appendChild(btn);

    card.appendChild(header);

    var body = document.createElement('div');
    body.className = 'cam-device-channels hidden';
    card.appendChild(body);

    return card;
}

/**
 * Cycle through common RTSP URLs for a camera until one probes successfully.
 * Renders progress in the device card and the Selected card on success.
 */
function _quickProbeCamera(cam, card) {
    var body = card.querySelector('.cam-device-channels');
    body.classList.remove('hidden');
    while (body.firstChild) body.removeChild(body.firstChild);
    var status = document.createElement('p');
    status.setAttribute('aria-busy', 'true');
    status.textContent = 'Trying common RTSP URLs...';
    body.appendChild(status);

    var creds = _getCamCredentials();

    fetch('/api/camera/common-urls?ip=' + encodeURIComponent(cam.ip) +
          '&username=' + encodeURIComponent(creds.username) +
          '&password=' + encodeURIComponent(creds.password) +
          '&scopes=' + encodeURIComponent(cam.scopes || '') +
          '&hardware=' + encodeURIComponent(cam.hardware || '') +
          '&name=' + encodeURIComponent(cam.name || ''))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var urls = data.urls || [];
            function tryNext(i) {
                if (i >= urls.length) {
                    setText(body,
                        'No working stream found. ' +
                        (creds.username ? 'Double-check the password, or' : 'Add a username/password above and') +
                        ' try again.');
                    return;
                }
                fetch('/api/camera/probe?url=' + encodeURIComponent(urls[i]))
                    .then(function(r) { return r.json(); })
                    .then(function(result) {
                        if (result.error) {
                            tryNext(i + 1);
                        } else {
                            _renderSelectedCamera(urls[i], result);
                            // Collapse the scan results — selection is shown above
                            var scanDiv = document.getElementById('scan-results');
                            if (scanDiv) scanDiv.classList.add('hidden');
                        }
                    })
                    .catch(function() { tryNext(i + 1); });
            }
            tryNext(0);
        })
        .catch(function() { setText(body, 'Failed to fetch RTSP URL candidates.'); });
}

/**
 * Probe an NVR for active channels and render a small selection table inline.
 */
function _detectChannels(cam, card) {
    var body = card.querySelector('.cam-device-channels');
    body.classList.remove('hidden');
    while (body.firstChild) body.removeChild(body.firstChild);
    var status = document.createElement('p');
    status.setAttribute('aria-busy', 'true');
    status.textContent = 'Probing channels (this may take a moment)...';
    body.appendChild(status);

    var creds = _getCamCredentials();

    fetch('/api/camera/detect-channels', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            ip: cam.ip,
            username: creds.username,
            password: creds.password,
            hardware: cam.hardware || '',
            name: cam.name || '',
            scopes: cam.scopes || '',
        })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        while (body.firstChild) body.removeChild(body.firstChild);
        if (!data.channels || data.channels.length === 0) {
            setText(body,
                'No active channels found. ' +
                (creds.username ? 'Check the password.' : 'Add credentials above and try again.'));
            return;
        }

        if (data.brand) {
            var brandP = document.createElement('p');
            brandP.innerHTML = '<small>Detected brand: <strong>' + data.brand + '</strong></small>';
            body.appendChild(brandP);
        }

        var table = document.createElement('table');
        var thead = document.createElement('thead');
        var hr = document.createElement('tr');
        ['Ch', 'Quality', 'Resolution', 'Codec', ''].forEach(function(h) {
            var th = document.createElement('th');
            th.textContent = h;
            hr.appendChild(th);
        });
        thead.appendChild(hr);
        table.appendChild(thead);

        var tbody = document.createElement('tbody');
        data.channels.forEach(function(ch) {
            var tr = document.createElement('tr');
            tr.appendChild(_td(ch.channel));
            tr.appendChild(_td(ch.quality === 'main' ? 'Main' : 'Sub'));
            tr.appendChild(_td(ch.resolution + ' @ ' + ch.framerate + 'fps'));
            tr.appendChild(_td(ch.video_codec + (ch.can_copy ? '' : ' (transcode)')));

            var btnTd = document.createElement('td');
            var selBtn = document.createElement('button');
            selBtn.type = 'button';
            selBtn.className = 'outline';
            selBtn.style.cssText = 'padding:0.25rem 0.7rem;font-size:0.85em;width:auto;margin:0';
            selBtn.textContent = 'Select';
            selBtn.addEventListener('click', (function(channel) {
                return function() {
                    _renderSelectedCamera(channel.url, channel);
                    var scanDiv = document.getElementById('scan-results');
                    if (scanDiv) scanDiv.classList.add('hidden');
                };
            })(ch));
            btnTd.appendChild(selBtn);
            tr.appendChild(btnTd);

            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        body.appendChild(table);
    })
    .catch(function() { setText(body, 'Channel detection failed.'); });
}

function _td(text) {
    var td = document.createElement('td');
    td.textContent = text;
    return td;
}
