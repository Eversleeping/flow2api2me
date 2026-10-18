/* Interactive login screen. Frames and input stay inside the admin session. */
(() => {
    const modal = document.createElement('div');
    modal.id = 'browserLoginModal';
    modal.style.cssText = 'display:none;position:fixed;inset:0;z-index:70;background:#000b;overflow:auto;padding:16px';
    modal.innerHTML = `
      <section style="max-width:1320px;margin:0 auto;background:white;border-radius:10px;padding:16px">
        <div class="flex flex-wrap items-center justify-between gap-3 mb-3">
          <h3 class="text-lg font-semibold">浏览器登录</h3>
          <div class="flex gap-2">
            <button id="browserLoginFlow" class="rounded border px-3 py-2 text-sm">打开 Flow</button>
            <button id="browserLoginSave" class="rounded bg-black text-white px-3 py-2 text-sm">保存登录态</button>
            <button id="browserLoginClose" class="rounded border px-3 py-2 text-sm">关闭</button>
          </div>
        </div>
        <p class="text-sm mb-2">在下方画面中点击并输入，完成 Google 登录和二次验证。进入 Flow 后点击“保存登录态”。</p>
        <p id="browserLoginAccount" class="text-sm mb-2"></p>
        <p id="browserLoginOrigin" class="text-sm mb-2"></p>
        <p id="browserLoginStatus" role="status" aria-live="polite" class="text-sm mb-3"></p>
        <div style="background:#f3f4f6;min-height:200px;overflow:hidden;border:1px solid #ddd;border-radius:6px">
          <img id="browserLoginScreen" tabindex="0" alt="服务器浏览器画面；点击后可以使用键盘输入" draggable="false"
               style="display:none;width:100%;height:auto;cursor:default;outline-offset:-3px;touch-action:pan-y">
        </div>
        <form id="browserLoginInputForm" class="flex flex-wrap items-center gap-2 mt-3">
          <input id="browserLoginText" type="password" autocomplete="off" aria-label="发送到浏览器的内容" maxlength="4096"
                 placeholder="也可在此输入邮箱、密码或验证码" class="border rounded px-3 py-2 text-sm flex-1" style="min-width:240px">
          <button class="rounded border px-3 py-2 text-sm" type="submit">输入到选中位置</button>
          <button id="browserLoginEnter" class="rounded border px-3 py-2 text-sm" type="button">回车</button>
          <button id="browserLoginTab" class="rounded border px-3 py-2 text-sm" type="button">Tab</button>
          <button id="browserLoginBackspace" class="rounded border px-3 py-2 text-sm" type="button">退格</button>
        </form>
        <p class="text-xs text-gray-500 mt-2">登录窗口最多保留 20 分钟。若 Google 提示此浏览器无法登录，可使用浏览器扩展同步登录态。</p>
      </section>`;
    document.body.appendChild(modal);
    const el = id => document.getElementById(id);
    const screen = el('browserLoginScreen');
    const status = el('browserLoginStatus');
    let sessionId = null, timer = null, frameUrl = null, saving = false;
    let inputQueue = Promise.resolve(), generation = 0;

    async function request(path, body) {
        const response = await fetch('/api/browser-login' + path, {
            method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
            headers: {'Content-Type': 'application/json', 'X-Browser-Login': '1'},
            ...(body === undefined ? {} : {body: JSON.stringify(body)}),
        });
        if (response.status === 401) { location.href = '/login'; throw new Error('后台登录已过期'); }
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            const error = new Error(typeof data.detail === 'string' ? data.detail : '浏览器请求失败，请重试');
            error.status = response.status;
            throw error;
        }
        return response;
    }
    function message(text, error = false) {
        status.textContent = text;
        status.style.color = error ? '#b91c1c' : '#374151';
    }
    function stopFrames() {
        generation++;
        clearTimeout(timer);
        if (frameUrl) URL.revokeObjectURL(frameUrl);
        frameUrl = null;
        screen.removeAttribute('src');
        screen.style.display = 'none';
    }
    function forget() {
        sessionId = null;
        sessionStorage.removeItem('flowBrowserLogin');
        stopFrames();
        el('browserLoginText').value = '';
        el('browserLoginOrigin').textContent = '';
    }
    async function poll(version = generation) {
        if (!sessionId || version !== generation || saving) return;
        const id = sessionId;
        try {
            const state = await (await request(`/${id}/status`)).json();
            if (version !== generation || id !== sessionId) return;
            el('browserLoginAccount').textContent = state.expected_email ? `请登录：${state.expected_email}` : '登录完成后会自动识别账号邮箱';
            el('browserLoginOrigin').textContent = state.page_origin ? `当前网页：${state.page_origin}` : '';
            el('browserLoginSave').disabled = state.state !== 'ready';
            if (state.state !== 'ready') {
                message(state.message, state.state === 'error');
                if (state.state === 'error' || state.state === 'closed') { forget(); return; }
            } else {
                if (!frameUrl) message(state.message);
                const response = await request(`/${id}/screenshot`);
                const blob = await response.blob();
                if (version !== generation || id !== sessionId) return;
                const next = URL.createObjectURL(blob), previous = frameUrl;
                frameUrl = next;
                screen.onload = () => { if (previous) URL.revokeObjectURL(previous); };
                screen.src = next;
                screen.style.display = 'block';
            }
        } catch (error) {
            if (version !== generation) return;
            message(error.message, true);
            if ([403, 404, 410].includes(error.status)) { forget(); return; }
        }
        if (version === generation && !saving) timer = setTimeout(() => poll(version), 1000);
    }
    window.openBrowserLogin = async (tokenId = null) => {
        modal.style.display = 'block';
        message('正在启动登录浏览器…');
        el('browserLoginSave').disabled = true;
        try {
            const data = await (await request('/start', {token_id: tokenId})).json();
            stopFrames();
            sessionId = data.id;
            sessionStorage.setItem('flowBrowserLogin', sessionId);
            message(data.message);
            poll();
        } catch (error) { message(error.message, true); }
    };
    function send(event) {
        const id = sessionId;
        if (!id || saving) return Promise.resolve();
        inputQueue = inputQueue.then(async () => {
            if (id !== sessionId || saving) return;
            await request(`/${id}/input`, event);
        }).catch(error => message(error.message, true));
        return inputQueue;
    }
    function position(event) {
        const box = screen.getBoundingClientRect();
        return {x: Math.max(0, Math.min(1279, (event.clientX - box.left) * 1280 / box.width)),
                y: Math.max(0, Math.min(799, (event.clientY - box.top) * 800 / box.height))};
    }
    screen.addEventListener('click', event => { screen.focus({preventScroll: true}); send({kind: 'click', ...position(event)}); });
    screen.addEventListener('wheel', event => {
        event.preventDefault();
        send({kind: 'wheel', ...position(event), delta: Math.max(-2000, Math.min(2000, event.deltaY))});
    }, {passive: false});
    screen.addEventListener('keydown', event => {
        if (event.isComposing) return;
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'v') return;
        const modifiers = (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
            event.preventDefault(); send({kind: 'key', key: 'a', modifiers: event.ctrlKey ? 2 : 4});
        } else if (['Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
            event.preventDefault(); send({kind: 'key', key: event.key, modifiers});
        } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
            event.preventDefault(); send({kind: 'text', text: event.key});
        }
    });
    screen.addEventListener('paste', event => {
        event.preventDefault();
        const text = event.clipboardData.getData('text/plain');
        if (text) send({kind: 'text', text: text.slice(0, 4096)});
    });
    el('browserLoginInputForm').addEventListener('submit', event => {
        event.preventDefault();
        const field = el('browserLoginText'), text = field.value;
        field.value = '';
        if (text) send({kind: 'text', text});
    });
    for (const key of ['Enter', 'Tab', 'Backspace']) el('browserLogin' + key).onclick = () => send({kind: 'key', key});
    el('browserLoginFlow').onclick = () => send({kind: 'flow'});
    el('browserLoginClose').onclick = async () => {
        if (saving) return;
        const id = sessionId;
        forget();
        modal.style.display = 'none';
        if (id) await request(`/${id}/cancel`, {}).catch(() => {});
    };
    el('browserLoginSave').onclick = async () => {
        if (!sessionId || saving) return;
        await inputQueue;
        saving = true;
        clearTimeout(timer);
        el('browserLoginSave').disabled = true;
        message('正在验证 Flow 登录态，请稍候…');
        try {
            const result = await (await request(`/${sessionId}/complete`, {})).json();
            forget();
            message(result.message);
            await refreshTokens();
            showToast(result.message || '登录态已保存，账号已启用', 'success');
        } catch (error) { message(error.message, true); }
        finally { saving = false; if (sessionId) { el('browserLoginSave').disabled = false; poll(); } }
    };
    const resume = sessionStorage.getItem('flowBrowserLogin');
    if (resume) {
        sessionId = resume;
        modal.style.display = 'block';
        message('正在恢复登录窗口…');
        poll();
    }
})();
