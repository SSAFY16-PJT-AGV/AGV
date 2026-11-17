"""Self-contained interface for the local virtual transport demonstration."""

HTML_PAGE = r'''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AGV 가상 운반 시연</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: #18243a; background: #f3f6fa; }
    * { box-sizing: border-box; }
    body { margin: 0; }
    main { max-width: 940px; margin: 0 auto; padding: 36px 20px 64px; }
    header { margin-bottom: 28px; }
    .eyebrow { margin: 0 0 8px; color: #2469a4; font-size: 0.8rem; font-weight: 800; letter-spacing: .09em; }
    h1 { margin: 0; font-size: clamp(1.7rem, 4vw, 2.35rem); line-height: 1.25; }
    .intro { color: #52647c; line-height: 1.65; margin: 12px 0 0; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; align-items: start; gap: 18px; }
    .card { background: white; border: 1px solid #dce4ee; border-radius: 18px; padding: 26px; box-shadow: 0 10px 28px rgba(33, 56, 83, .05); }
    h2 { font-size: 1.15rem; margin: 0 0 8px; }
    .help { margin: 0 0 24px; color: #64748b; font-size: .92rem; line-height: 1.5; }
    label { display: block; margin: 19px 0 8px; font-size: .9rem; font-weight: 700; }
    select, button { width: 100%; min-height: 48px; font: inherit; border-radius: 9px; }
    select { padding: 0 12px; color: #18243a; border: 1px solid #cbd6e4; background: white; }
    select:focus-visible, button:focus-visible { outline: 3px solid #94c4fb; outline-offset: 2px; }
    button { margin-top: 24px; border: 0; background: #1e62a1; color: white; font-weight: 700; cursor: pointer; }
    button:hover:not(:disabled) { background: #184e80; }
    button:disabled { cursor: not-allowed; opacity: .55; }
    .feedback { min-height: 1.5em; margin: 13px 0 0; font-size: .9rem; line-height: 1.5; color: #a52a2a; }
    .feedback.success { color: #186345; }
    .badge { display: inline-block; background: #e9f2fa; color: #185b91; padding: 6px 10px; border-radius: 99px; font-size: .85rem; font-weight: 700; }
    dl { margin: 20px 0 0; }
    dl div { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 12px; border-top: 1px solid #edf1f5; padding: 15px 0; }
    dt { color: #64748b; font-size: .9rem; }
    dd { margin: 0; font-size: .95rem; overflow-wrap: anywhere; }
    .notice { margin: 20px 0 0; padding: 14px 16px; border-radius: 10px; background: #f0f6fb; color: #486079; font-size: .87rem; line-height: 1.6; }
    .fleet-row { white-space: pre-line; line-height: 1.7; border-top: 1px solid #edf1f5; padding: 12px 0; overflow-wrap: anywhere; }
    @media (max-width: 660px) { main { padding: 26px 16px 48px; } .grid { grid-template-columns: 1fr; } .card { padding: 21px; } }
  </style>
</head>
<body>
<main>
  <header>
    <p class="eyebrow">AGV DIGITAL TWIN</p>
    <h1>가상 운반 시연</h1>
    <p class="intro">상품을 선택하면 서버가 경로를 계산합니다.<br>Unreal에서 가상 AGV의 운반을 확인하세요.</p>
  </header>
  <div class="grid">
    <section class="card" aria-labelledby="order-title">
      <h2 id="order-title">상품 주문</h2>
      <p class="help">시연할 가상 상품을 선택하세요.</p>
      <form id="product-form">
        <label for="product">상품</label>
        <select id="product" name="product_id" required disabled><option value="">상품을 불러오는 중</option></select>
        <p id="product-details" class="help" aria-live="polite">상품을 선택하면 픽업·목적지와 선반을 표시합니다.</p>
        <button id="submit-product" type="submit" disabled>가상 상품 주문</button>
      </form>
      <p id="product-feedback" class="feedback" role="status" aria-live="polite"></p>
      <ul id="submission-list" aria-live="polite"></ul>
      <details id="direct-orders">
      <summary>노드 직접 선택</summary>
      <p class="help">가상 운반 지점을 직접 지정할 때 사용합니다.</p>
      <form id="order-form">
        <label for="pickup">출발지</label>
        <select id="pickup" name="pickup_node" required disabled><option value="">노드를 불러오는 중</option></select>
        <label for="destination">도착지</label>
        <select id="destination" name="destination_node" required disabled><option value="">노드를 불러오는 중</option></select>
        <button id="submit-order" type="submit" disabled>가상 운반 요청</button>
      </form>
      <p id="order-feedback" class="feedback" role="status" aria-live="polite"></p>
      </details>
    </section>
    <section class="card" aria-labelledby="status-title">
      <h2 id="status-title">진행 현황</h2>
      <p class="help">서버가 보고하는 가상 운반 상태입니다.</p>
      <div id="single-status">
      <span class="badge" id="status">확인 중</span>
      <dl aria-live="polite">
        <div><dt>단계</dt><dd id="phase">확인 중</dd></div>
        <div><dt>현재 노드</dt><dd id="current-node">—</dd></div>
        <div><dt>계산 경로</dt><dd id="route">—</dd></div>
      </dl>
      </div>
      <div id="fleet-status" aria-live="polite" hidden></div>
      <p id="status-feedback" class="feedback" role="status" aria-live="polite"></p>
      <p class="notice">이 화면은 가상 시연입니다. 실제 상품이나 로봇 운행 결과는 아닙니다.</p>
    </section>
  </div>
</main>
<script>
(() => {
  'use strict';
  const pickup = document.getElementById('pickup');
  const destination = document.getElementById('destination');
  const submit = document.getElementById('submit-order');
  const feedback = document.getElementById('order-feedback');
  const product = document.getElementById('product');
  const productSubmit = document.getElementById('submit-product');
  const productFeedback = document.getElementById('product-feedback');
  const productDetails = document.getElementById('product-details');
  const statusFeedback = document.getElementById('status-feedback');
  let nodesReady = false;
  let productsReady = false;
  let products = new Map();
  let submitting = false;
  let polling = false;
  let jobActive = true;
  let fleetMode = false;
  const pendingSubmissions = new Map();
  let submissionUnknown = false;

  function message(element, text, success = false) {
    element.textContent = text;
    element.classList.toggle('success', success);
  }
  async function readJson(response) {
    if (!response.ok) throw new Error('request failed');
    return await response.json();
  }
  function updateButton() {
    const awaiting = (!fleetMode && pendingSubmissions.size > 0) || submissionUnknown;
    submit.disabled = !nodesReady || submitting || jobActive || awaiting;
    productSubmit.disabled = !productsReady || submitting || jobActive || awaiting;
  }
  function makeRequestKey() {
    if (typeof crypto === 'undefined') throw new Error('secure randomness is unavailable');
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    if (typeof crypto.getRandomValues !== 'function') throw new Error('secure randomness is unavailable');
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
  }
  function submissionFailed(element, entry = null) {
    submissionUnknown = true;
    message(element, '요청 결과를 확인할 수 없습니다. 재주문하지 말고 서버 상태를 확인해 주세요.');
    if (entry) entry.row.textContent = `${entry.label} · 요청 결과를 확인할 수 없습니다.`;
    updateButton();
  }
  async function refreshSubmission(entry) {
    if (!pendingSubmissions.has(entry.id) || entry.polling) return;
    entry.polling = true;
    try {
      const result = await readJson(await fetch('/order-demo/api/submissions/' + encodeURIComponent(entry.id), {cache: 'no-store'}));
      if (!result || result.request_id !== entry.id) throw new Error('invalid submission status');
      if (result.status === 'pending') {
        message(entry.feedback, '가상 주문 처리 결과를 확인 중입니다.');
        entry.row.textContent = `${entry.label} · 접수 결과 확인 중 (${entry.id})`;
      } else if (result.status === 'completed' && result.result &&
                 !['unknown', 'failed', 'error'].includes(result.result.status)) {
        pendingSubmissions.delete(entry.id);
        const slot = Number.isInteger(result.result.slot_index) ? ` · ${result.result.slot_index}번 슬롯` : '';
        entry.row.textContent = `${entry.label} · 접수 완료${slot} (${entry.id})`;
        message(entry.feedback, entry.success, true);
        await refreshStatus();
      } else if (result.status === 'error' && typeof result.error_type === 'string' && result.error_type) {
        pendingSubmissions.delete(entry.id);
        const reason = result.error_type === 'ShelfFull' ? '선반에 빈 슬롯이 없습니다' : '주문이 접수되지 않았습니다';
        entry.row.textContent = `${entry.label} · ${reason} (${entry.id})`;
        message(entry.feedback, reason + '.');
        await refreshStatus();
      } else if (result.status === 'unknown' || result.status === 'completed') {
        pendingSubmissions.delete(entry.id);
        submissionFailed(entry.feedback, entry);
      } else {
        throw new Error('invalid submission status');
      }
    } catch (_) {
      message(entry.feedback, '주문 상태를 확인하지 못했습니다. 재주문하지 마세요. 자동으로 다시 확인합니다.');
      entry.row.textContent = `${entry.label} · 상태 조회 실패, 다시 확인 중 (${entry.id})`;
    } finally {
      entry.polling = false;
      updateButton();
    }
  }
  async function refreshSubmissions() {
    await Promise.all(Array.from(pendingSubmissions.values(), entry => refreshSubmission(entry)));
  }
  async function submitRequest(url, body, element, success, label) {
    let requestKey;
    try { requestKey = makeRequestKey(); }
    catch (_) { message(element, '주문 요청 식별자를 만들 수 없어 접수하지 않았습니다.'); return; }
    try {
      const response = await fetch(url, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': requestKey},
        body: JSON.stringify(body)
      });
      if (response.status === 503) {
        message(element, '현재 주문이 접수되지 않았습니다. 잠시 후 다시 시도해 주세요.');
        return;
      }
      if (response.status === 409) {
        const rejected = await response.json();
        if (rejected && rejected.detail === '선반에 빈 슬롯이 없습니다') {
          const row = document.createElement('li');
          row.textContent = `${label} · 선반에 빈 슬롯이 없습니다`;
          document.getElementById('submission-list').appendChild(row);
          message(element, '선반에 빈 슬롯이 없습니다.');
          await refreshStatus();
          return;
        }
        submissionFailed(element);
        return;
      }
      const result = await readJson(response);
      if (result && result.status === 'pending' && typeof result.request_id === 'string' &&
          /^[A-Za-z0-9_-]{1,128}$/.test(result.request_id)) {
        if (pendingSubmissions.has(result.request_id)) {
          submissionFailed(element);
          return;
        }
        const row = document.createElement('li');
        const entry = {id: result.request_id, feedback: element, success, row, label, polling: false};
        row.textContent = `${label} · 접수 결과 확인 중 (${entry.id})`;
        document.getElementById('submission-list').appendChild(row);
        pendingSubmissions.set(entry.id, entry);
        message(element, '가상 주문 처리 결과를 확인 중입니다.');
        updateButton();
        if (fleetMode) void refreshSubmission(entry);
        else await refreshSubmission(entry);
      } else if (!result || ['pending', 'unknown', 'failed', 'error'].includes(result.status)) {
        submissionFailed(element);
      } else {
        if (!fleetMode) jobActive = true;
        message(element, success, true);
        await refreshStatus();
      }
    } catch (_) {
      submissionFailed(element);
    }
  }
  function showProductDetails() {
    const item = products.get(product.value);
    productDetails.textContent = item
      ? `픽업 ${item.pickup_node} → 목적지 ${item.destination_node} · 선반 ${item.shelf_id} ${Number.isInteger(item.slot_index) ? `${item.slot_index}번 슬롯` : '빈 슬롯 자동 배정'}`
      : '상품을 선택하면 픽업·목적지와 선반을 표시합니다.';
  }
  async function loadProducts() {
    try {
      const data = await readJson(await fetch('/order-demo/api/products', {cache: 'no-store'}));
      if (!Array.isArray(data.products) || !data.products.length ||
          !data.products.every(item => item && ['id', 'label', 'pickup_node', 'destination_node', 'shelf_id']
            .every(key => typeof item[key] === 'string' && item[key])) ||
          new Set(data.products.map(item => item.id)).size !== data.products.length) {
        throw new Error('invalid products');
      }
      products = new Map(data.products.map(item => [item.id, item]));
      product.replaceChildren();
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = '상품을 선택하세요';
      product.appendChild(placeholder);
      for (const item of data.products) {
        const option = document.createElement('option');
        option.value = item.id;
        option.textContent = item.label;
        product.appendChild(option);
      }
      product.disabled = false;
      productsReady = true;
      updateButton();
    } catch (_) {
      message(productFeedback, '상품 목록을 불러오지 못했습니다. 서버 상태를 확인하고 새로고침해 주세요.');
    }
  }
  product.addEventListener('change', showProductDetails);
  function addNodes(select, nodes) {
    select.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '노드를 선택하세요';
    select.appendChild(placeholder);
    for (const node of nodes) {
      const option = document.createElement('option');
      option.value = node.id;
      option.textContent = node.type ? `${node.id} (${node.type})` : node.id;
      select.appendChild(option);
    }
    select.disabled = false;
  }
  async function loadNodes() {
    try {
      const data = await readJson(await fetch('/order-demo/api/nodes', {cache: 'no-store'}));
      if (!Array.isArray(data.nodes) || !data.nodes.length ||
          !data.nodes.every(node => node && typeof node.id === 'string' && node.id)) throw new Error('invalid nodes');
      addNodes(pickup, data.nodes);
      addNodes(destination, data.nodes);
      nodesReady = true;
      updateButton();
    } catch (_) {
      message(feedback, '노드 목록을 불러오지 못했습니다. 서버 상태를 확인하고 새로고침해 주세요.');
    }
  }
  const statuses = {idle: '대기 중', pending: '요청 접수', queued: '요청 접수',
    running: '이동 중', moving: '이동 중', completed: '가상 도착', arrived: '가상 도착',
    waiting: '확인 중', waiting_reservation: '구간 예약 대기', reservation_wait: '구간 예약 대기',
    waiting_grab: '집기 확인 중', waiting_place: '적재 확인 중',
    unknown: '결과 확인 불가', failed: '작업 실패', error: '오류'};
  const phases = {idle: '대기 중', pending: '경로 준비 중', to_pickup: '출발지로 이동',
    pickup: '출발지 도착', to_destination: '도착지로 이동', completed: '가상 도착', done: '가상 도착',
    waiting_reservation: '구간 예약 대기', reservation_wait: '구간 예약 대기',
    waiting_grab: '집기 확인 중', waiting_place: '적재 확인 중', unknown: '결과 확인 불가',
    arrived: '가상 도착', failed: '처리 실패'};
  function textValue(value) { return typeof value === 'string' && value ? value : '—'; }
  async function refreshStatus() {
    if (polling) return;
    polling = true;
    try {
      const data = await readJson(await fetch('/order-demo/api/status', {cache: 'no-store'}));
      if (data.stale !== undefined && typeof data.stale !== 'boolean') throw new Error('invalid freshness');
      const stale = data.stale === true;
      const disconnected = data.connected === false || data.unreal_connected === false;
      fleetMode = data.mode === 'fleet';
      document.getElementById('single-status').hidden = fleetMode;
      document.getElementById('fleet-status').hidden = !fleetMode;
      if (fleetMode) {
        if (typeof data.accepting_orders !== 'boolean' ||
            (data.accepting_queued_orders !== undefined && typeof data.accepting_queued_orders !== 'boolean') ||
            !Array.isArray(data.robots) ||
            !data.robots.every(robot => robot && typeof robot.robot_id === 'string' && robot.robot_id)) {
          throw new Error('invalid fleet status');
        }
        document.getElementById('direct-orders').hidden = true;
        const fleet = document.getElementById('fleet-status');
        fleet.replaceChildren();
        for (const robot of data.robots) {
          const row = document.createElement('p');
          row.className = 'fleet-row';
          const phase = phases[robot.phase] || textValue(robot.phase);
          const effectiveStatus = robot.status === 'waiting' && robot.phase === 'reservation_wait'
            ? 'reservation_wait' : robot.status;
          const status = robot.status === 'completed' && robot.product_type && robot.phase === 'done'
            ? '가상 적재 확인됨' : (statuses[effectiveStatus] || textValue(effectiveStatus));
          const route = Array.isArray(robot.route) && robot.route.length
            ? robot.route.map(textValue).join(' → ') : '—';
          row.textContent = `${robot.robot_id} · ${status}\n단계: ${phase}\n현재 노드: ${textValue(robot.current_node)}\n경로: ${route}`;
          fleet.appendChild(row);
        }
        const canQueue = data.accepting_queued_orders === undefined ? data.accepting_orders : data.accepting_queued_orders;
        const unresolved = data.robots.some(robot => ['unknown', 'failed'].includes(robot.status));
        jobActive = stale || disconnected || unresolved || !canQueue;
        updateButton();
        message(statusFeedback, stale ? '표시된 내용은 최신 상태가 아닙니다. 새 주문은 잠시 기다려 주세요.'
          : (disconnected ? '서버 또는 Unreal 연결을 확인해 주세요. 새 주문은 잠시 기다려 주세요.'
            : (jobActive ? '현재 새로운 가상 주문을 받을 수 없습니다.' : '')));
        return;
      }
      const terminalIssue = ['unknown', 'failed'].includes(data.status) ? data.status : null;
      const exhausted = data.mode === 'coordinated' && data.orders_remaining === 0 && data.status === 'completed';
      document.getElementById('direct-orders').hidden = data.mode === 'coordinated';
      const pendingPhase = ['waiting_grab', 'waiting_place', 'unknown', 'failed'].includes(data.phase);
      const effectiveStatus = terminalIssue || (pendingPhase ? data.phase : data.status);
      const effectivePhase = terminalIssue || data.phase;
      const confirmedProduct = !!data.product_type && data.status === 'completed' && data.phase === 'done';
      document.getElementById('status').textContent = confirmedProduct ? '가상 적재 확인됨' : (statuses[effectiveStatus] || textValue(effectiveStatus));
      document.getElementById('phase').textContent = confirmedProduct ? '가상 적재 확인됨' : (phases[effectivePhase] || textValue(effectivePhase));
      document.getElementById('current-node').textContent = textValue(data.current_node);
      document.getElementById('route').textContent = Array.isArray(data.route) && data.route.length
        ? data.route.map(textValue).join(' → ') : '—';
      jobActive = stale || disconnected || exhausted || ['moving', 'running', 'waiting_grab', 'waiting_place', 'unknown', 'failed'].includes(effectiveStatus);
      updateButton();
      message(statusFeedback, stale ? '표시된 내용은 최신 상태가 아닙니다. 새 주문은 잠시 기다려 주세요.'
        : (disconnected ? '서버 또는 Unreal 연결을 확인해 주세요. 새 주문은 잠시 기다려 주세요.'
          : (terminalIssue || exhausted ? '시연 서버와 Unreal 에디터를 종료한 뒤 다시 시작해 주세요.' : '')));
    } catch (_) {
      jobActive = true; updateButton();
      message(statusFeedback, '진행 현황을 불러오지 못했습니다. 서버 연결을 확인해 주세요.');
    } finally { polling = false; }
  }
  document.getElementById('product-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!productsReady || submitting || jobActive || (!fleetMode && pendingSubmissions.size) || submissionUnknown) return;
    if (!products.has(product.value)) {
      message(productFeedback, '상품을 선택해 주세요.');
      return;
    }
    submitting = true;
    updateButton();
    message(productFeedback, '');
    try { await submitRequest('/order-demo/api/product-orders', {product_id: product.value},
                              productFeedback, '가상 상품 주문을 접수했습니다.', products.get(product.value).label); }
    finally { submitting = false; updateButton(); }
  });
  document.getElementById('order-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!nodesReady || submitting || jobActive || pendingSubmissions.size || submissionUnknown) return;
    if (!pickup.value || !destination.value) {
      message(feedback, '출발지와 도착지를 선택해 주세요.');
      return;
    }
    if (pickup.value === destination.value) {
      message(feedback, '출발지와 도착지를 다르게 선택해 주세요.');
      return;
    }
    submitting = true;
    updateButton();
    message(feedback, '');
    try { await submitRequest('/order-demo/api/orders', {pickup_node: pickup.value, destination_node: destination.value},
                              feedback, '가상 운반 요청을 접수했습니다.', `${pickup.value} → ${destination.value}`); }
    finally { submitting = false; updateButton(); }
  });
  loadNodes();
  loadProducts();
  refreshStatus();
  setInterval(async () => { await Promise.all([refreshStatus(), refreshSubmissions()]); }, 800);
})();
</script>
</body>
</html>'''
