(function () {
    'use strict';

    var form = document.getElementById('worker-form');
    var runBtn = document.getElementById('run-btn');
    var spinner = runBtn.querySelector('.spinner');
    var statusBox = document.getElementById('status');
    var resultBox = document.getElementById('result');
    var resultInfo = document.getElementById('result-info');
    var downloadBtn = document.getElementById('download-btn');
    var logsWrap = document.getElementById('logs-wrap');
    var logsPre = document.getElementById('logs');
    var historyWrap = document.getElementById('history-wrap');
    var historyList = document.getElementById('history');

    function showStatus(text, type) {
        statusBox.textContent = text;
        statusBox.className = type || '';
        statusBox.classList.remove('hidden');
    }

    function hideStatus() {
        statusBox.className = 'hidden';
    }

    function showLogs(logs) {
        if (!logs || !logs.length) {
            logsWrap.classList.add('hidden');
            return;
        }
        logsPre.textContent = logs.join('
');
        logsWrap.classList.remove('hidden');
        logsPre.scrollTop = logsPre.scrollHeight;
    }

    function clearFieldErrors() {
        ['api_url', 'archive_url', 'password', 'prompt'].forEach(function (name) {
            var el = document.getElementById('err-' + name);
            if (el) { el.textContent = ''; }
        });
    }

    function setBusy(busy) {
        runBtn.disabled = busy;
        spinner.classList.toggle('hidden', !busy);
        runBtn.querySelector('.btn-label').textContent =
            busy ? 'Обработка, ожидайте...' : 'Запустить переработку';
    }

    function formatSize(bytes) {
        if (bytes < 1024) { return bytes + ' Б'; }
        if (bytes < 1024 * 1024) { return (bytes / 1024).toFixed(1) + ' КБ'; }
        return (bytes / 1024 / 1024).toFixed(2) + ' МБ';
    }

    function loadHistory() {
        fetch('/results')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.archives || !data.archives.length) {
                    historyWrap.classList.add('hidden');
                    return;
                }
                historyList.innerHTML = '';
                data.archives.forEach(function (name) {
                    var li = document.createElement('li');
                    var a = document.createElement('a');
                    a.href = '/download/' + encodeURIComponent(name);
                    a.textContent = '⬇ ' + name;
                    li.appendChild(a);
                    historyList.appendChild(li);
                });
                historyWrap.classList.remove('hidden');
            })
            .catch(function () { /* история недоступна — не критично */ });
    }

    form.addEventListener('submit', function (event) {
        event.preventDefault();
        clearFieldErrors();
        resultBox.classList.add('hidden');
        hideStatus();

        var payload = {
            api_url: document.getElementById('api_url').value,
            archive_url: document.getElementById('archive_url').value,
            model: document.getElementById('model').value,
            password: document.getElementById('password').value,
            prompt: document.getElementById('prompt').value
        };

        setBusy(true);
        showStatus('Идет обработка: скачивание архива, запрос к ИИ, применение правок. Это может занять несколько минут...', 'busy');

        fetch('/api/process', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
            .then(function (response) {
                return response.json().then(function (data) {
                    return { status: response.status, data: data };
                });
            })
            .then(function (res) {
                setBusy(false);
                showLogs(res.data.logs);

                if (res.data.ok) {
                    hideStatus();
                    resultInfo.textContent = res.data.filename + ' (' + formatSize(res.data.size) + ')';
                    downloadBtn.href = res.data.download_url;
                    downloadBtn.setAttribute('download', res.data.filename);
                    resultBox.classList.remove('hidden');
                    loadHistory();
                } else {
                    showStatus('Ошибка: ' + (res.data.error || 'неизвестная ошибка'), 'error');
                    if (res.data.fields) {
                        Object.keys(res.data.fields).forEach(function (name) {
                            var el = document.getElementById('err-' + name);
                            if (el) { el.textContent = res.data.fields[name]; }
                        });
                    }
                }
            })
            .catch(function (err) {
                setBusy(false);
                showStatus('Ошибка соединения с сервером: ' + err.message, 'error');
            });
    });

    loadHistory();
})();
