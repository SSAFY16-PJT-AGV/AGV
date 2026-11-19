"""Self-contained interface for the local virtual transport demonstration."""

HTML_PAGE = r'''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SMART MART | 가상 물류 관제</title>
  <style>
    :root { color-scheme: light; font-family: "Segoe UI", "Malgun Gothic", system-ui, sans-serif; color: #1b302a; background: #f2f5f0; font-synthesis: none; }
    * { box-sizing: border-box; }
    body { margin: 0; }
    [hidden] { display: none !important; }
    .topbar { background: #153d32; color: #f7fff9; }
    .topbar-inner { max-width: 1440px; margin: auto; min-height: 70px; padding: 0 48px; display: flex; align-items: center; justify-content: space-between; gap: 20px; }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .brand-mark { display: grid; place-items: center; width: 37px; height: 37px; border-radius: 12px; background: #d5ef93; color: #153d32; font-weight: 900; font-size: 1.2rem; }
    .brand-name { font-weight: 800; letter-spacing: .04em; font-size: .95rem; }
    .brand-sub { display: block; color: #b9d2c3; font-size: .62rem; letter-spacing: .15em; margin-top: 2px; }
    .top-status { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .top-pill { border: 1px solid #5b8070; border-radius: 99px; padding: 7px 12px; font-size: .75rem; color: #e0f2e5; font-weight: 700; }
    .top-pill::before { content: ''; display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #e6cb90; margin-right: 7px; vertical-align: 1px; }
    .top-pill.is-ready::before { background: #d5ef93; }
    main { max-width: 1440px; margin: 0 auto; padding: 40px 48px 72px; }
    .page-heading { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin-bottom: 26px; }
    .eyebrow { margin: 0 0 9px; color: #53766a; font-size: .7rem; font-weight: 800; letter-spacing: .16em; }
    h1 { margin: 0; font-size: clamp(1.75rem, 3vw, 2.6rem); letter-spacing: -.045em; line-height: 1.2; }
    .intro { color: #61756b; line-height: 1.6; margin: 10px 0 0; font-size: .95rem; }
    .page-tag { padding: 8px 12px; border: 1px solid #cbd9cf; border-radius: 8px; color: #527163; font-size: .75rem; font-weight: 700; white-space: nowrap; }
    .overview { display: flex; align-items: center; gap: 18px; margin-bottom: 22px; padding: 21px 25px; border-radius: 16px; background: #e0eedf; border: 1px solid #d2e4d1; }
    .overview-mark { display: grid; place-items: center; width: 40px; height: 40px; flex: 0 0 auto; border-radius: 12px; background: #f8fcf6; color: #345f4c; font-size: 1.2rem; }
    .overview strong { display: block; font-size: .95rem; }
    .overview p { margin: 4px 0 0; color: #537363; font-size: .82rem; line-height: 1.5; }
    .grid { display: grid; grid-template-columns: minmax(0, .9fr) minmax(0, 1.2fr) minmax(0, 1.2fr); align-items: start; gap: 20px; }
    .card { min-width: 0; background: #fff; border: 1px solid #dce6dc; border-radius: 18px; padding: 26px; box-shadow: 0 8px 26px rgba(25, 60, 42, .035); }
    .card-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
    .section-kicker { margin: 0 0 8px; color: #729281; font-size: .67rem; font-weight: 800; letter-spacing: .13em; }
    h2 { font-size: 1.18rem; letter-spacing: -.035em; margin: 0 0 7px; }
    .help { margin: 0 0 24px; color: #71857a; font-size: .83rem; line-height: 1.6; }
    .section-icon { display: grid; place-items: center; flex: 0 0 auto; width: 38px; height: 38px; border-radius: 12px; background: #edf5e8; color: #32634b; font-weight: 800; }
    label { display: block; margin: 19px 0 8px; font-size: .82rem; font-weight: 750; }
    select, button { width: 100%; min-height: 49px; font: inherit; border-radius: 10px; }
    select { padding: 0 12px; color: #203b2f; border: 1px solid #d7e2d9; background: #fbfdfb; }
    select:focus-visible, button:focus-visible, summary:focus-visible { outline: 3px solid #78b596; outline-offset: 2px; }
    button { margin-top: 16px; border: 0; background: #256348; color: white; font-weight: 750; cursor: pointer; }
    button:hover:not(:disabled) { background: #174d36; }
    button:disabled { cursor: not-allowed; opacity: .5; }
    .feedback { min-height: 1.2em; margin: 10px 0 0; font-size: .82rem; line-height: 1.5; color: #a33327; }
    .feedback.success { color: #186345; }
    #product-details { margin: 10px 0 0; color: #75887c; }
    #submission-list, #saved-order-list { padding-left: 20px; color: #52695a; line-height: 1.8; font-size: .82rem; overflow-wrap: anywhere; }
    #submission-list:empty { display: none; }
    details { margin-top: 24px; padding-top: 17px; border-top: 1px solid #edf1ec; }
    summary { color: #517160; font-size: .83rem; font-weight: 700; cursor: pointer; }
    details .help { margin: 12px 0 0; }
    .badge { display: inline-block; background: #edf6e9; color: #286543; padding: 6px 10px; border-radius: 99px; font-size: .8rem; font-weight: 700; }
    dl { margin: 16px 0 0; }
    dl div { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 12px; border-top: 1px solid #edf1ec; padding: 15px 0; }
    dt { color: #72857a; font-size: .82rem; }
    dd { margin: 0; font-size: .9rem; overflow-wrap: anywhere; }
    .steps { margin: 18px 0 6px; padding: 15px 16px; border-radius: 12px; background: #f6f9f5; border: 1px solid #edf2eb; }
    .steps-label { margin: 0 0 10px; color: #668171; font-size: .72rem; font-weight: 750; }
    .step-list { display: flex; flex-wrap: wrap; gap: 6px; color: #41614f; font-size: .76rem; font-weight: 700; }
    .step-list span:not(:last-child)::after { content: '→'; color: #9db3a3; padding-left: 6px; }
    .phase-track { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 12px; }
    .phase-step { display: inline-block; padding: 4px 7px; border-radius: 6px; background: #edf2ed; color: #65786a; font-size: .7rem; line-height: 1.3; }
    .phase-step.current { background: #256348; color: #fff; font-weight: 800; }
    .fleet-row { white-space: pre-line; line-height: 1.8; padding: 14px 16px; margin: 10px 0; border: 1px solid #e4ece4; background: #fbfdfb; border-radius: 12px; overflow-wrap: anywhere; color: #355243; font-size: .88rem; }
    .notice { margin: 16px 0 0; padding: 12px 14px; border-radius: 10px; background: #f5f7f4; color: #708277; font-size: .76rem; line-height: 1.6; }
    #map-card { min-width: 0; }
    .map-stage { min-height: 210px; border: 1px solid #e4ece4; border-radius: 12px; background: #f7faf6; display: grid; place-items: center; overflow: hidden; }
    .map-stage svg { display: block; width: 100%; height: 300px; }
    .map-caption { margin: 12px 0 0; color: #708277; font-size: .77rem; line-height: 1.55; overflow-wrap: anywhere; }
    .map-legend { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 14px 0 0; color: #526b5c; font-size: .75rem; line-height: 1.5; }
    .map-legend span::before { content: ''; display: inline-block; margin-right: 6px; vertical-align: middle; }
    .map-legend .legend-robot::before { width: 10px; height: 10px; border-radius: 50%; background: #3467ae; }
    .map-legend .legend-occupied::before { width: 16px; height: 4px; border-radius: 2px; background: #cb8c36; }
    .map-legend .legend-path::before { width: 16px; height: 4px; border-radius: 2px; background: #a4b9a9; }
    #saved-orders { margin-top: 20px; }
    #saved-inventory { margin: 0; padding: 15px 16px; border-radius: 10px; background: #f5f9f3; color: #496456; overflow-wrap: anywhere; }
    #inventory-freshness { font-size: .75rem; color: #688173; margin: 0 0 12px; }
    @media (max-width: 1180px) { .grid { grid-template-columns: minmax(0, .8fr) minmax(0, 1.2fr); } #map-card { grid-column: 1 / -1; } }
    @media (max-width: 760px) { .topbar-inner { padding: 15px 20px; } main { padding: 30px 20px 52px; } .grid { grid-template-columns: minmax(0, 1fr); } #map-card { grid-column: auto; } .page-heading { align-items: start; } .card { padding: 22px; } }
    @media (max-width: 440px) { .brand-sub, .page-tag { display: none; } .topbar-inner { align-items: flex-start; } .top-status { gap: 5px; } .top-pill { padding: 6px 8px; font-size: .68rem; } main { padding-inline: 15px; } .overview { padding: 17px; } }
  </style>
</head>
<body>
<header class="topbar"><div class="topbar-inner">
  <div class="brand"><span class="brand-mark" aria-hidden="true">S</span><span class="brand-name">SMART MART<span class="brand-sub">LOGISTICS CONTROL</span></span></div>
  <div class="top-status" aria-live="polite"><span id="dashboard-source" class="top-pill">가상 시연 · 확인 중</span><span id="dashboard-health" class="top-pill">연결 확인 중</span></div>
</div></header>
<main>
  <div class="page-heading"><div>
    <p class="eyebrow">SMART LOGISTICS / OVERVIEW</p>
    <h1>물류 관제 대시보드</h1>
    <p class="intro">상품을 선택하고 가상 운반 과정을 확인하세요.</p>
  </div><span class="page-tag">가상 운반 시연</span></div>
  <div class="overview"><div class="overview-mark" aria-hidden="true">↗</div><div><strong>상품 보충, 한눈에</strong><p>주문 접수부터 가상 로봇의 운반과 선반 상태까지 확인합니다.</p></div></div>
  <div class="grid">
    <section class="card" aria-labelledby="order-title">
      <div class="card-top"><div><p class="section-kicker">01 / NEW REQUEST</p><h2 id="order-title">상품 보충 요청</h2></div><span class="section-icon" aria-hidden="true">＋</span></div>
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
      <div class="card-top"><div><p class="section-kicker">02 / FLEET STATUS</p><h2 id="status-title">로봇 운반 현황</h2></div><span class="section-icon" aria-hidden="true">↗</span></div>
      <p class="help">서버가 보고하는 가상 운반 상태입니다.</p>
      <div class="steps" aria-label="가상 상품 주문 단계 안내"><p class="steps-label">현재 단계만 강조합니다 · 완료 이력이나 시각은 제공되지 않습니다</p><div class="step-list"><span>접수·배정</span><span>픽업 이동</span><span>집기</span><span>배송 이동</span><span>적재</span><span>완료</span></div></div>
      <div id="single-status">
      <span class="badge" id="status">확인 중</span>
      <div id="single-timeline" class="phase-track" aria-label="가상 주문 현재 단계"></div>
      <dl aria-live="polite">
        <div><dt>단계</dt><dd id="phase">확인 중</dd></div>
        <div><dt id="current-node-label">현재 노드</dt><dd id="current-node">—</dd></div>
        <div><dt>계산 경로</dt><dd id="route">—</dd></div>
      </dl>
      </div>
      <div id="fleet-status" aria-live="polite" hidden></div>
      <p id="status-feedback" class="feedback" role="status" aria-live="polite"></p>
      <p class="notice">이 화면은 가상 시연입니다. 실제 상품이나 로봇 운행 결과는 아닙니다.</p>
    </section>
  <section class="card" id="map-card" aria-labelledby="map-title">
    <div class="card-top"><div><p class="section-kicker">03 / WAREHOUSE MAP</p><h2 id="map-title">가상 창고 맵</h2></div><span class="section-icon" aria-hidden="true">⌖</span></div>
    <p class="help">서버의 가상 맵과 점유 정보를 확인합니다.</p>
    <div class="map-stage"><p id="map-feedback" class="help" role="status">맵 정보 확인 중</p><svg id="dashboard-map" role="img" aria-label="가상 창고 맵" hidden></svg></div>
    <div class="map-legend" aria-label="맵 범례"><span class="legend-robot">파란 원 · 가상 로봇 위치</span><span class="legend-occupied">주황색 · 가상 점유</span><span class="legend-path">회색 · 일반 경로</span></div>
    <p class="map-caption" id="map-details">가상 맵의 점유는 실제 로봇의 위치나 안전 상태를 뜻하지 않습니다.</p>
  </section>
  </div>
  <section class="card" id="saved-orders" hidden>
    <div class="card-top"><div><p class="section-kicker">04 / INVENTORY & ORDERS</p><h2>재고와 주문 내역</h2></div><span class="section-icon" aria-hidden="true">▦</span></div>
    <p class="help">가상 주문 내역과 선반 상태를 확인합니다.</p>
    <p id="inventory-freshness" role="status">재고 확인 중</p>
    <p id="saved-inventory" class="help"></p>
    <ul id="saved-order-list" aria-live="polite"></ul>
  </section>
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
  let mapPolling = false;
  let mapGeneration = 0;
  let mapStarted = 0;
  let mapController = null;
  let statusGeneration = 0;
  let statusStarted = 0;
  let statusController = null;
  let savedPolling = false;
  let savedGeneration = 0;
  let savedStarted = 0;
  let savedController = null;
  let mapRobotLayer = null;
  let mapBounds = null;
  let lastMapStatus = null;
  let activeTimelines = [];
  const pendingSubmissions = new Map();
  let submissionUnknown = false;
  let activePost = null;
  let postDelayUnknown = false;

  function message(element, text, success = false) {
    element.textContent = text;
    element.classList.toggle('success', success);
  }
  async function readJson(response) {
    if (!response.ok) throw new Error('request failed');
    return await response.json();
  }
  function observationClock() {
    return typeof performance !== 'undefined' && typeof performance.now === 'function'
      ? performance.now() : Date.now();
  }
  function observationExpired(start, limit) {
    const now = observationClock();
    return !Number.isFinite(now) || now < start || now - start >= limit;
  }
  function makeController() {
    return typeof AbortController === 'function' ? new AbortController() : null;
  }
  function observationOptions(controller) {
    return controller ? {cache: 'no-store', signal: controller.signal} : {cache: 'no-store'};
  }
  function hasUncertainSubmission() {
    return Array.from(pendingSubmissions.values()).some(entry => entry.lookupUncertain === true);
  }
  function updateButton() {
    const awaiting = (!fleetMode && pendingSubmissions.size > 0) || hasUncertainSubmission() ||
                     submissionUnknown || postDelayUnknown;
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
    message(element, '요청 결과를 확인할 수 없습니다. 새로고침 후에도 서버 확인 전에는 재주문하지 마세요.');
    if (entry) entry.row.textContent = `${entry.label} · 요청 결과를 확인할 수 없습니다.`;
    updateButton();
  }
  function markLookupDelayed(entry) {
    entry.lookupUncertain = true;
    message(entry.feedback, '접수 결과 확인이 지연되고 있습니다. 자동으로 다시 확인합니다. 새로고침 후에도 서버 확인 전에는 재주문하지 마세요.');
    entry.row.textContent = `${entry.label} · 접수 결과 확인 지연 (${entry.id})`;
    updateButton();
  }
  async function refreshSubmission(entry) {
    if (!pendingSubmissions.has(entry.id)) return;
    if (entry.polling) {
      if (observationExpired(entry.started, 5000)) {
        ++entry.generation;
        if (entry.controller) entry.controller.abort();
        entry.controller = null;
        entry.polling = false;
        markLookupDelayed(entry);
      }
      return;
    }
    entry.polling = true;
    entry.started = observationClock();
    const generation = entry.generation = (entry.generation || 0) + 1;
    const controller = entry.controller = makeController();
    try {
      const result = await readJson(await fetch('/order-demo/api/submissions/' + encodeURIComponent(entry.id), observationOptions(controller)));
      if (generation !== entry.generation || !pendingSubmissions.has(entry.id)) return;
      if (observationExpired(entry.started, 5000)) {
        ++entry.generation;
        entry.controller = null;
        entry.polling = false;
        markLookupDelayed(entry);
        return;
      }
      if (!result || result.request_id !== entry.id) throw new Error('invalid submission status');
      if (result.status === 'pending') {
        message(entry.feedback, entry.lookupUncertain
          ? '기존 주문의 처리 결과를 확인 중입니다. 새로고침 후에도 서버 확인 전에는 재주문하지 마세요.'
          : '가상 주문 처리 결과를 확인 중입니다.');
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
        const reason = result.error_type === 'ShelfFull' ? '선반에 빈 슬롯이 없습니다'
          : (result.error_type === 'PickupUnavailable' ? '픽업 재고가 없거나 확인되지 않았습니다' : '주문이 접수되지 않았습니다');
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
      if (generation === entry.generation && pendingSubmissions.has(entry.id)) {
        entry.lookupUncertain = true;
        message(entry.feedback, '주문 상태를 확인하지 못했습니다. 자동으로 다시 확인합니다. 새로고침 후에도 서버 확인 전에는 재주문하지 마세요.');
        entry.row.textContent = `${entry.label} · 상태 조회 실패, 다시 확인 중 (${entry.id})`;
      }
    } finally {
      if (generation === entry.generation) { entry.polling = false; entry.controller = null; }
      updateButton();
    }
  }
  async function refreshSubmissions() {
    if (activePost && !activePost.warned && observationExpired(activePost.started, 5000)) {
      activePost.warned = true;
      postDelayUnknown = true;
      message(activePost.element, '요청 결과를 확인할 수 없습니다. 새로고침 후에도 서버 확인 전에는 재주문하지 마세요.');
      updateButton();
    }
    await Promise.all(Array.from(pendingSubmissions.values(), entry => refreshSubmission(entry)));
  }
  async function submitRequest(url, body, element, success, label) {
    let requestKey;
    try { requestKey = makeRequestKey(); }
    catch (_) { message(element, '주문 요청 식별자를 만들 수 없어 접수하지 않았습니다.'); return; }
    const attempt = activePost = {started: observationClock(), element, warned: false};
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
    } finally {
      if (activePost === attempt) { activePost = null; postDelayUnknown = false; }
      updateButton();
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
    waiting_grab: '집기 확인 중', waiting_place: '적재 확인 중', suspended: '연결 중단',
    unknown: '결과 확인 불가', failed: '작업 실패', error: '오류'};
  const phases = {idle: '대기 중', pending: '경로 준비 중', to_pickup: '출발지로 이동',
    pickup: '출발지 도착', to_destination: '도착지로 이동', completed: '가상 도착', done: '가상 도착',
    waiting_reservation: '구간 예약 대기', reservation_wait: '구간 예약 대기',
    waiting_grab: '집기 확인 중', waiting_place: '적재 확인 중', unknown: '결과 확인 불가',
    arrived: '가상 도착', failed: '처리 실패', connection_lost: '연결 끊김'};
  function textValue(value) { return typeof value === 'string' && value ? value : '—'; }
  function renderTimeline(container, robot, fresh) {
    container.replaceChildren();
    const labels=['접수·배정','픽업 이동','집기','배송 이동','적재','완료'];
    const indices={pending:0,to_pickup:1,waiting_grab:2,to_destination:3,waiting_place:4};
    let active=null;
    if (fresh && robot && !['unknown','failed','error','suspended'].includes(robot.status)) {
      if (robot.phase === 'done' && robot.status === 'completed' && robot.product_type) active=5;
      else if (Object.prototype.hasOwnProperty.call(indices,robot.phase)) active=indices[robot.phase];
    }
    labels.forEach((label,index)=>{
      const step=document.createElement('span');
      step.className='phase-step' + (index === active ? ' current' : '');
      step.textContent=label;
      if (index === active) step.setAttribute('aria-current','step');
      container.appendChild(step);
    });
  }
  function svgElement(type, attributes, label) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', type);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
    if (label !== undefined) element.textContent = label;
    return element;
  }
  function renderMapRobots(data) {
    if (!mapRobotLayer || !mapBounds) return;
    mapRobotLayer.replaceChildren();
    // Robot status and map occupancy are independent snapshots. Never mark an
    // unknown or stale pose as the current position.
    if (!data || data.stale !== false || data.connected !== true || data.unreal_connected !== true) return;
    const robots = data.mode === 'fleet' ? data.robots : [data];
    if (!Array.isArray(robots)) return;
    for (const robot of robots) {
      if (!robot || typeof robot.status !== 'string' ||
          ['unknown', 'failed', 'error', 'suspended'].includes(robot.status)) continue;
      const pose = robot && robot.pose;
      if (!pose || !Number.isFinite(pose.x) || !Number.isFinite(pose.y) ||
          pose.x < mapBounds.minX || pose.x > mapBounds.maxX ||
          pose.y < mapBounds.minY || pose.y > mapBounds.maxY) continue;
      const id = typeof robot.robot_id === 'string' && robot.robot_id ? robot.robot_id : '가상 로봇';
      const visibleId = id.length > 24 ? id.slice(0, 24) + '…' : id;
      const marker = svgElement('g', {});
      const circle = svgElement('circle', {cx:pose.x, cy:-pose.y, r:mapBounds.unit * .018,
                                          fill:'#3467ae', stroke:'#fff', 'stroke-width':mapBounds.unit * .006});
      circle.appendChild(svgElement('title', {}, id));
      marker.appendChild(circle);
      marker.appendChild(svgElement('text', {x:pose.x, y:-pose.y-mapBounds.unit*.032,
        fill:'#234d86', stroke:'#f7faf6', 'stroke-width':mapBounds.unit*.006,
        'paint-order':'stroke', 'stroke-linejoin':'round', 'text-anchor':'middle',
        'font-size':mapBounds.unit*.026, 'font-family':'sans-serif', 'font-weight':'700'}, visibleId));
      mapRobotLayer.appendChild(marker);
    }
  }
  function renderMapSnapshot(data) {
    if (!data || data.scope !== 'isolated_simulation' || data.coordinate_unit !== 'mm' ||
        data.snapshot_consistency !== 'separate_from_status' || typeof data.map_id !== 'string' ||
        !Array.isArray(data.nodes) || !data.nodes.length || data.nodes.length > 2000 ||
        !Array.isArray(data.edges) || data.edges.length > 4000 || !Array.isArray(data.occupancy) ||
        data.occupancy.length > 10000) throw new Error('invalid map snapshot');
    const validPoint = p => Array.isArray(p) && p.length === 2 && p.every(v =>
      typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= 1000000000);
    const ids = new Set();
    const points = [];
    let pointCount=0;
    for (const node of data.nodes) {
      if (!node || typeof node.id !== 'string' || !node.id || ids.has(node.id) || !validPoint(node.pos))
        throw new Error('invalid map node');
      ids.add(node.id); points.push(node.pos);
    }
    for (const edge of data.edges) {
      if (!edge || !ids.has(edge.from) || !ids.has(edge.to) || !Array.isArray(edge.shape) ||
          edge.shape.length < 2 || edge.shape.length > 10000 || !edge.shape.every(validPoint))
        throw new Error('invalid map edge');
      pointCount += edge.shape.length;
      if (pointCount > 100000) throw new Error('map has too many points');
      points.push(...edge.shape);
    }
    for (const item of data.occupancy) {
      if (!item || !['node','edge','zone'].includes(item.kind) ||
          typeof item.resource_id !== 'string' || typeof item.robot_id !== 'string')
        throw new Error('invalid map occupancy');
    }
    let minX=Infinity, maxX=-Infinity, minY=Infinity, maxY=-Infinity;
    for (const [x,y] of points) { minX=Math.min(minX,x); maxX=Math.max(maxX,x); minY=Math.min(minY,y); maxY=Math.max(maxY,y); }
    const unit=Math.max(maxX-minX,maxY-minY,100), pad=unit*.12;
    const svg=document.getElementById('dashboard-map');
    svg.replaceChildren();
    svg.setAttribute('viewBox', `${minX-pad} ${-maxY-pad} ${maxX-minX+2*pad} ${maxY-minY+2*pad}`);
    const edges=svgElement('g', {'stroke-linecap':'round','stroke-linejoin':'round',fill:'none'});
    const nodes=svgElement('g', {});
    const occupiedNodes=new Set(data.occupancy.filter(item=>item.kind==='node').map(item=>item.resource_id));
    const occupiedEdges=new Set();
    for (const item of data.occupancy.filter(item=>item.kind==='edge')) {
      // Python may JSON-escape non-ASCII IDs; compare decoded pairs, not raw JSON.
      const pair=JSON.parse(item.resource_id);
      if (!Array.isArray(pair) || pair.length !== 2 || !pair.every(id=>typeof id==='string'))
        throw new Error('invalid occupied edge');
      occupiedEdges.add(JSON.stringify(pair.sort()));
    }
    for (const edge of data.edges) {
      // The reservation store encodes an undirected edge as a sorted JSON pair.
      const occupied=occupiedEdges.has(JSON.stringify([edge.from,edge.to].sort()));
      const element=svgElement('polyline', {points:edge.shape.map(p=>`${p[0]},${-p[1]}`).join(' '),
                                            stroke:occupied?'#cb8c36':'#a4b9a9','stroke-width':unit*(occupied ? .014 : .008)});
      element.appendChild(svgElement('title', {}, `${edge.from} → ${edge.to}`));
      edges.appendChild(element);
    }
    for (const node of data.nodes) {
      const occupied=occupiedNodes.has(node.id);
      const circle=svgElement('circle',{cx:node.pos[0],cy:-node.pos[1],r:unit*.012,
        fill:occupied?'#cb8c36':'#456f57',stroke:'#fff','stroke-width':unit*.004});
      circle.appendChild(svgElement('title',{},node.id)); nodes.appendChild(circle);
      nodes.appendChild(svgElement('text',{x:node.pos[0]+unit*.016,y:-node.pos[1]-unit*.014,
        fill:'#426251','font-size':unit*.025,'font-family':'sans-serif'},node.id));
    }
    const layer=svgElement('g',{});
    svg.appendChild(edges); svg.appendChild(nodes); svg.appendChild(layer);
    mapRobotLayer=layer; mapBounds={minX,maxX,minY,maxY,unit};
    renderMapRobots(lastMapStatus);
    svg.hidden=false;
    document.getElementById('map-feedback').hidden=true;
    const resources=data.occupancy.slice(0,12).map(item=>`${item.kind} ${item.resource_id} (${item.robot_id})`);
    const suffix=data.occupancy.length>12 ? ` 외 ${data.occupancy.length-12}건` : '';
    document.getElementById('map-details').textContent = `${data.map_id} · 가상 DB 점유: ${resources.length ? resources.join(' · ')+suffix : '없음'} · 로봇 위치와 점유는 서로 다른 시점의 가상 관측입니다.`;
  }
  function mapUnavailable() {
    // Do not preserve an old snapshot as if it were a current empty map.
    mapRobotLayer=null; mapBounds=null;
    document.getElementById('dashboard-map').hidden=true;
    const feedback=document.getElementById('map-feedback');
    feedback.hidden=false; feedback.textContent='가상 맵과 점유 정보를 확인할 수 없습니다.';
    document.getElementById('map-details').textContent='맵 연결을 확인해 주세요. 점유 상태는 알 수 없습니다.';
  }
  async function refreshMap() {
    if (mapPolling) {
      if (observationExpired(mapStarted, 5000)) {
        ++mapGeneration;
        if (mapController) mapController.abort();
        mapController=null; mapPolling=false;
        mapUnavailable();
      }
      return;
    }
    mapPolling=true; mapStarted=observationClock();
    const generation=++mapGeneration, controller=mapController=makeController();
    try {
      const data=await readJson(await fetch('/order-demo/api/map',observationOptions(controller)));
      if (generation !== mapGeneration) return;
      if (observationExpired(mapStarted, 5000)) { mapUnavailable(); return; }
      renderMapSnapshot(data);
    } catch (_) {
      if (generation === mapGeneration) mapUnavailable();
    } finally {
      if (generation === mapGeneration) { mapPolling=false; mapController=null; }
    }
  }
  async function refreshSavedOrders() {
    if (savedPolling) {
      if (observationExpired(savedStarted, 5000)) {
        ++savedGeneration;
        if (savedController) savedController.abort();
        savedController=null; savedPolling=false;
        document.getElementById('inventory-freshness').textContent = '재고·주문 연결 확인 필요 · 표시 값은 마지막 조회 결과';
      }
      return;
    }
    savedPolling=true; savedStarted=observationClock();
    const generation=++savedGeneration, controller=savedController=makeController();
    try {
      const response = await fetch('/order-demo/api/orders', observationOptions(controller));
      if (generation !== savedGeneration) return;
      if (observationExpired(savedStarted, 5000)) {
        document.getElementById('inventory-freshness').textContent = '재고·주문 연결 확인 필요 · 표시 값은 마지막 조회 결과';
        return;
      }
      if (response.status === 404) throw new Error('saved orders unavailable');
      const data = await readJson(response);
      if (generation !== savedGeneration) return;
      if (observationExpired(savedStarted, 5000)) {
        document.getElementById('inventory-freshness').textContent = '재고·주문 연결 확인 필요 · 표시 값은 마지막 조회 결과';
        return;
      }
      if (!Array.isArray(data.orders)) throw new Error('invalid saved orders');
      const labels = {queued: '대기', dispatching: '배정 중', active: '진행 중',
                      completed: '적재 완료', failed: '실패', unknown: '결과 확인 필요'};
      const rows = [];
      for (const order of data.orders) {
        const row = document.createElement('li');
        const item = products.get(order.product_id);
        const slot = Number.isInteger(order.slot_index) ? ` · ${order.shelf_id} ${order.slot_index}번 슬롯` : '';
        row.textContent = `${item ? item.label : order.product_id} · ${labels[order.status] || order.status}${slot} (${order.request_id})`;
        rows.push(row);
      }
      const inventory = await readJson(await fetch('/order-demo/api/inventory', observationOptions(controller)));
      if (generation !== savedGeneration) return;
      if (observationExpired(savedStarted, 5000)) {
        document.getElementById('inventory-freshness').textContent = '재고·주문 연결 확인 필요 · 표시 값은 마지막 조회 결과';
        return;
      }
      const list = document.getElementById('saved-order-list');
      list.replaceChildren();
      for (const row of rows) list.appendChild(row);
      document.getElementById('inventory-freshness').textContent = inventory.fresh === true
        ? '현재 가상 세계에서 확인된 재고' : '마지막 저장 재고 · 현재 상태 확인 필요';
      const parts = Object.entries(inventory.shelves || {}).map(([name, shelf]) =>
        `${name}: ${shelf.slots.filter(value => value === true).length}/${shelf.slots.length}칸 적재`);
      if (inventory.pickup && inventory.pickup.sources) {
        for (const [name, source] of Object.entries(inventory.pickup.sources)) {
          const count = Number.isInteger(source.available) ? `${source.available}개` : '확인 전';
          const reserved = Number.isInteger(source.reserved) ? `${source.reserved}개` : '확인 전';
          parts.push(`${name} 픽업: ${count} (예약 ${reserved})${inventory.pickup.fresh ? '' : ' [저장값]'}`);
        }
      }
      document.getElementById('saved-inventory').textContent = `${parts.join(' · ')}${inventory.fresh ? '' : ' (마지막 저장값)'}`;
      document.getElementById('saved-orders').hidden = false;
    } catch (_) {
      // Previously fetched values are not proof that inventory is current.
      if (generation === savedGeneration)
        document.getElementById('inventory-freshness').textContent = '재고·주문 연결 확인 필요 · 표시 값은 마지막 조회 결과';
    } finally {
      if (generation === savedGeneration) { savedPolling=false; savedController=null; }
    }
  }
  function statusUnavailable() {
    jobActive = true; updateButton();
    for (const item of activeTimelines) renderTimeline(item.element, item.robot, false);
    renderTimeline(document.getElementById('single-timeline'), {}, false);
    activeTimelines=[];
    const fleet=document.getElementById('fleet-status');
    fleet.replaceChildren();
    const notice=document.createElement('div');
    notice.className='fleet-row';
    notice.textContent='로봇 상태를 확인할 수 없습니다.';
    fleet.appendChild(notice);
    document.getElementById('status').textContent='상태 확인 불가';
    document.getElementById('phase').textContent='확인 불가';
    document.getElementById('current-node-label').textContent='위치';
    document.getElementById('current-node').textContent='—';
    document.getElementById('route').textContent='—';
    lastMapStatus=null;
    renderMapRobots(null);
    const health = document.getElementById('dashboard-health');
    health.textContent = '상태 확인 불가';
    health.classList.toggle('is-ready', false);
    message(statusFeedback, '진행 현황을 불러오지 못했습니다. 서버 연결을 확인해 주세요.');
  }
  async function refreshStatus() {
    if (polling) {
      if (observationExpired(statusStarted, 3000)) {
        ++statusGeneration;
        if (statusController) statusController.abort();
        statusController=null; polling=false;
        statusUnavailable();
      }
      return;
    }
    polling = true; statusStarted=observationClock();
    const generation=++statusGeneration, controller=statusController=makeController();
    try {
      const data = await readJson(await fetch('/order-demo/api/status', observationOptions(controller)));
      if (generation !== statusGeneration) return;
      if (observationExpired(statusStarted, 3000)) { statusUnavailable(); return; }
      if (!data || typeof data.stale !== 'boolean' || typeof data.connected !== 'boolean' ||
          typeof data.unreal_connected !== 'boolean') throw new Error('invalid status freshness or connection');
      const stale = data.stale === true;
      const disconnected = data.connected === false || data.unreal_connected === false;
      lastMapStatus=data;
      renderMapRobots(data);
      const synthetic = data.action_source === 'synthetic';
      const timelineFresh = !stale && data.stale === false && data.connected === true && data.unreal_connected === true;
      document.getElementById('dashboard-source').textContent = synthetic ? '합성 세계 · 가상' : 'Unreal · 가상';
      const health = document.getElementById('dashboard-health');
      const ready = !stale && data.connected === true && data.unreal_connected === true;
      health.textContent = stale ? '최신 상태 확인 필요' : (disconnected ? '연결 확인 필요'
        : (ready ? '연결됨' : '연결 상태 확인 중'));
      health.classList.toggle('is-ready', ready);
      if (synthetic) document.querySelector('.intro').textContent =
        '상품을 선택하면 서버가 경로를 계산합니다. 합성 세계에서 가상 운반을 시연합니다.';
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
        activeTimelines = [];
        for (const robot of data.robots) {
          const row = document.createElement('div');
          row.className = 'fleet-row';
          const phase = phases[robot.phase] || textValue(robot.phase);
          const effectiveStatus = robot.status === 'waiting' && robot.phase === 'reservation_wait'
            ? 'reservation_wait' : robot.status;
          const status = robot.status === 'completed' && robot.product_type && robot.phase === 'done'
            ? '가상 적재 확인됨' : (statuses[effectiveStatus] || textValue(effectiveStatus));
          const route = Array.isArray(robot.route) && robot.route.length
            ? robot.route.map(textValue).join(' → ') : '—';
          const robotFresh = timelineFresh && !['unknown','failed','error','suspended'].includes(robot.status);
          const observed = robotFresh ? '' : ' · 마지막 관측 (현재 확인 불가)';
          const nodeLabel = robotFresh ? '현재 노드' : '마지막 관측 노드';
          row.textContent = `${robot.robot_id} · ${status}${observed}\n단계: ${phase}\n${nodeLabel}: ${textValue(robot.current_node)}\n경로: ${route}`;
          const timeline = document.createElement('div');
          timeline.className='phase-track';
          timeline.setAttribute('aria-label', `${robot.robot_id} 가상 주문 현재 단계`);
          renderTimeline(timeline, robot, timelineFresh);
          activeTimelines.push({element:timeline, robot});
          row.appendChild(timeline);
          fleet.appendChild(row);
        }
        const canQueue = data.accepting_queued_orders === undefined ? data.accepting_orders : data.accepting_queued_orders;
        const unresolved = data.robots.some(robot => ['unknown', 'failed'].includes(robot.status));
        jobActive = stale || disconnected || unresolved || !canQueue;
        updateButton();
        message(statusFeedback, stale ? '표시된 내용은 최신 상태가 아닙니다. 새 주문은 잠시 기다려 주세요.'
          : (disconnected ? (synthetic ? '합성 시연이 중단됐습니다. 저장된 상태를 확인해 주세요.' : '서버 또는 Unreal 연결을 확인해 주세요. 새 주문은 잠시 기다려 주세요.')
            : (data.recovery_required ? (synthetic ? '이전 가상 작업을 확인해야 합니다. 새 주문은 대기합니다.' : '이전 작업 또는 Unreal 상태를 확인해야 합니다. 새 주문은 대기합니다.')
            : (jobActive ? '현재 새로운 가상 주문을 받을 수 없습니다.' : ''))));
        return;
      }
      const terminalIssue = ['unknown', 'failed'].includes(data.status) ? data.status : null;
      const exhausted = data.mode === 'coordinated' && data.orders_remaining === 0 && data.status === 'completed';
      document.getElementById('direct-orders').hidden = data.mode === 'coordinated';
      const pendingPhase = ['waiting_grab', 'waiting_place', 'unknown', 'failed'].includes(data.phase);
      const effectiveStatus = terminalIssue || (pendingPhase ? data.phase : data.status);
      const effectivePhase = terminalIssue || data.phase;
      renderTimeline(document.getElementById('single-timeline'), data, timelineFresh);
      const confirmedProduct = !!data.product_type && data.status === 'completed' && data.phase === 'done';
      document.getElementById('status').textContent = confirmedProduct ? '가상 적재 확인됨' : (statuses[effectiveStatus] || textValue(effectiveStatus));
      document.getElementById('phase').textContent = confirmedProduct ? '가상 적재 확인됨' : (phases[effectivePhase] || textValue(effectivePhase));
      const singleFresh = timelineFresh && !['unknown','failed','error','suspended'].includes(data.status);
      document.getElementById('current-node-label').textContent = singleFresh ? '현재 노드' : '마지막 관측 노드';
      document.getElementById('current-node').textContent = textValue(data.current_node);
      document.getElementById('route').textContent = Array.isArray(data.route) && data.route.length
        ? data.route.map(textValue).join(' → ') : '—';
      jobActive = stale || disconnected || exhausted || ['moving', 'running', 'waiting_grab', 'waiting_place', 'unknown', 'failed'].includes(effectiveStatus);
      updateButton();
      message(statusFeedback, stale ? '표시된 내용은 최신 상태가 아닙니다. 새 주문은 잠시 기다려 주세요.'
        : (disconnected ? '서버 또는 Unreal 연결을 확인해 주세요. 새 주문은 잠시 기다려 주세요.'
          : (terminalIssue || exhausted ? '시연 서버와 Unreal 에디터를 종료한 뒤 다시 시작해 주세요.' : '')));
    } catch (_) {
      if (generation === statusGeneration) statusUnavailable();
    } finally {
      if (generation === statusGeneration) { polling=false; statusController=null; }
    }
  }
  document.getElementById('product-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!productsReady || submitting || jobActive || (!fleetMode && pendingSubmissions.size) ||
        hasUncertainSubmission() || submissionUnknown || postDelayUnknown) return;
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
    if (!nodesReady || submitting || jobActive || pendingSubmissions.size || hasUncertainSubmission() ||
        submissionUnknown || postDelayUnknown) return;
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
  refreshSavedOrders();
  refreshMap();
  setInterval(async () => { await Promise.all([refreshSavedOrders(), refreshMap()]); }, 3000);
  setInterval(async () => { await Promise.all([refreshStatus(), refreshSubmissions()]); }, 800);
})();
</script>
</body>
</html>'''
