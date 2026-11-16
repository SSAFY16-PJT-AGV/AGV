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
      <details>
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
      <span class="badge" id="status">확인 중</span>
      <dl aria-live="polite">
        <div><dt>단계</dt><dd id="phase">확인 중</dd></div>
        <div><dt>현재 노드</dt><dd id="current-node">—</dd></div>
        <div><dt>계산 경로</dt><dd id="route">—</dd></div>
      </dl>
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
  let jobActive = false;

  function message(element, text, success = false) {
    element.textContent = text;
    element.classList.toggle('success', success);
  }
  async function readJson(response) {
    if (!response.ok) throw new Error('request failed');
    return await response.json();
  }
  function updateButton() {
    submit.disabled = !nodesReady || submitting || jobActive;
    productSubmit.disabled = !productsReady || submitting || jobActive;
  }
  function showProductDetails() {
    const item = products.get(product.value);
    productDetails.textContent = item
      ? `픽업 ${item.pickup_node} → 목적지 ${item.destination_node} · 선반 ${item.shelf_id} ${item.slot_index}번 슬롯`
      : '상품을 선택하면 픽업·목적지와 선반을 표시합니다.';
  }
  async function loadProducts() {
    try {
      const data = await readJson(await fetch('/order-demo/api/products', {cache: 'no-store'}));
      if (!Array.isArray(data.products) || !data.products.length ||
          !data.products.every(item => item && ['id', 'label', 'pickup_node', 'destination_node', 'shelf_id']
            .every(key => typeof item[key] === 'string' && item[key]) &&
            Number.isInteger(item.slot_index) && item.slot_index >= 0) ||
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
    waiting_grab: '집기 확인 중', waiting_place: '적재 확인 중',
    unknown: '결과 확인 불가', failed: '작업 실패', error: '오류'};
  const phases = {idle: '대기 중', pending: '경로 준비 중', to_pickup: '출발지로 이동',
    pickup: '출발지 도착', to_destination: '도착지로 이동', completed: '가상 도착', done: '가상 도착',
    waiting_grab: '집기 확인 중', waiting_place: '적재 확인 중', unknown: '결과 확인 불가',
    arrived: '가상 도착', failed: '처리 실패'};
  function textValue(value) { return typeof value === 'string' && value ? value : '—'; }
  async function refreshStatus() {
    if (polling) return;
    polling = true;
    try {
      const data = await readJson(await fetch('/order-demo/api/status', {cache: 'no-store'}));
      const terminalIssue = ['unknown', 'failed'].includes(data.status) ? data.status : null;
      const pendingPhase = ['waiting_grab', 'waiting_place', 'unknown', 'failed'].includes(data.phase);
      const effectiveStatus = terminalIssue || (pendingPhase ? data.phase : data.status);
      const effectivePhase = terminalIssue || data.phase;
      const confirmedProduct = !!data.product_type && data.status === 'completed' && data.phase === 'done';
      document.getElementById('status').textContent = confirmedProduct ? '가상 적재 확인됨' : (statuses[effectiveStatus] || textValue(effectiveStatus));
      document.getElementById('phase').textContent = confirmedProduct ? '가상 적재 확인됨' : (phases[effectivePhase] || textValue(effectivePhase));
      document.getElementById('current-node').textContent = textValue(data.current_node);
      document.getElementById('route').textContent = Array.isArray(data.route) && data.route.length
        ? data.route.map(textValue).join(' → ') : '—';
      jobActive = ['moving', 'running', 'waiting_grab', 'waiting_place', 'unknown', 'failed'].includes(effectiveStatus);
      updateButton();
      message(statusFeedback, terminalIssue
        ? '시연 서버와 Unreal 에디터를 종료한 뒤 다시 시작해 주세요.' : '');
    } catch (_) {
      message(statusFeedback, '진행 현황을 불러오지 못했습니다. 서버 연결을 확인해 주세요.');
    } finally { polling = false; }
  }
  document.getElementById('product-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!productsReady || submitting || jobActive) return;
    if (!products.has(product.value)) {
      message(productFeedback, '상품을 선택해 주세요.');
      return;
    }
    submitting = true;
    updateButton();
    message(productFeedback, '');
    try {
      await readJson(await fetch('/order-demo/api/product-orders', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({product_id: product.value})
      }));
      jobActive = true;
      message(productFeedback, '가상 상품 주문을 접수했습니다.', true);
      await refreshStatus();
    } catch (_) {
      message(productFeedback, '상품 주문에 실패했습니다. 진행 중인 작업과 서버 상태를 확인해 주세요.');
    } finally { submitting = false; updateButton(); }
  });
  document.getElementById('order-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!nodesReady || submitting || jobActive) return;
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
    try {
      await readJson(await fetch('/order-demo/api/orders', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pickup_node: pickup.value, destination_node: destination.value})
      }));
      jobActive = true;
      message(feedback, '가상 운반 요청을 접수했습니다.', true);
      await refreshStatus();
    } catch (_) {
      message(feedback, '운반 요청에 실패했습니다. 진행 중인 작업과 서버 상태를 확인해 주세요.');
    } finally { submitting = false; updateButton(); }
  });
  loadNodes();
  loadProducts();
  refreshStatus();
  setInterval(refreshStatus, 800);
})();
</script>
</body>
</html>'''
